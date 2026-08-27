from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from deepseek_harness import DeepSeekHarness, Notification
from deepseek_harness.errors import (
    JsonRpcError,
    SdkProtocolError,
    TransportClosedError,
)

from witty_agent_server.infra.clients import dsh_client as dsh_client_module
from witty_agent_server.infra.clients.dsh_client import DshClient, DshClientError

_SESSION_KEY = "agent:1:session:9"
_SESSION_ID = "agent-1-session-9"
_MESSAGE_ID = "msg-001"


def _session_event(
    session_id: str,
    event_type: str,
    data: dict[str, Any] | None = None,
    *,
    seq: int = 0,
) -> Notification:
    return Notification(
        method="session.event",
        payload={
            "sessionId": session_id,
            "event": {"type": event_type, "seq": seq, "time": 0, "data": data or {}},
        },
    )


def _status(session_id: str, status: str) -> Notification:
    return Notification(
        method="session.status",
        payload={"sessionId": session_id, "status": status},
    )


def _receipt(session_id: str, message_id: str) -> Notification:
    return _session_event(
        session_id,
        "agent/inbox/spliced",
        {
            "target": "next-turn",
            "start": 0,
            "inserted": [
                {
                    "content": [{"type": "text", "text": "question"}],
                    "source": {"kind": "user"},
                    "role": "user",
                    "id": message_id,
                }
            ],
        },
    )


def _delta(session_id: str, text: str, *, seq: int = 0) -> Notification:
    return _session_event(
        session_id,
        "assistant/chunk",
        {
            "turn": 1,
            "step": 1,
            "chunk": {"type": "text-delta", "index": 0, "text": text},
        },
        seq=seq,
    )


def _message(session_id: str, text: str, *, seq: int) -> Notification:
    return _session_event(
        session_id,
        "assistant/message",
        {
            "turn": 1,
            "step": 1,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        },
        seq=seq,
    )


def _turn_end(session_id: str, *, seq: int, turn: int = 1) -> Notification:
    return _session_event(
        session_id, "turn/end", {"turn": turn, "reason": {"kind": "completed"}}, seq=seq
    )


def _full_turn_script(
    session_id: str, message_id: str
) -> list[Notification | BaseException]:
    """receipt → running → turn/start → delta → message → turn/end → idle。"""
    return [
        _receipt(session_id, message_id),
        _status(session_id, "running"),
        _session_event(session_id, "turn/start", {"turn": 1}, seq=1),
        _delta(session_id, "2", seq=2),
        _message(session_id, "2", seq=3),
        _turn_end(session_id, seq=4),
        _status(session_id, "idle"),
    ]


def _event_types(events: list[dict[str, Any]]) -> list[str]:
    return [
        e["payload"]["event"]["type"] for e in events if e["method"] == "session.event"
    ]


class _ScriptedSubscription:
    """``NotificationSubscription`` fake：脚本化 drain() + close 追踪。

    drain 对齐 SDK：非阻塞弹出全部就绪项，异常项直接 raise，队列空静默
    返回；``on_drain`` 钩子每次 drain 开头触发，供测试在停滞窗口注入 abort。
    """

    def __init__(
        self,
        items: list[Notification | BaseException],
        *,
        on_drain: Callable[[], None] | None = None,
    ) -> None:
        self._items = list(items)
        self._on_drain = on_drain
        self.closed = False
        self.drain_calls = 0

    @property
    def pending(self) -> int:
        return len(self._items)

    def drain(self, on_notification: Callable[[Notification], None]) -> None:
        self.drain_calls += 1
        if self._on_drain is not None:
            self._on_drain()
        while self._items:
            item = self._items.pop(0)
            if isinstance(item, BaseException):
                raise item
            on_notification(item)

    def close(self) -> None:
        self.closed = True


class _FakeHarnessClient:
    """``HarnessClient`` + ``DeepSeekHarness`` 合一 fake：回放脚本并记录调用。

    ``.client`` 自引用以满足 DshClient 对 duck-type harness 的访问。
    """

    def __init__(
        self,
        *turn_scripts: list[Notification | BaseException],
        on_drain: Callable[[], None] | None = None,
    ) -> None:
        self.client = self
        self.closed = False
        self._scripts = list(turn_scripts)
        self._on_drain = on_drain
        self.subscriptions: list[_ScriptedSubscription] = []
        self.ops: list[tuple[str, str]] = []
        self.prompt_calls: list[tuple[str, list[dict[str, Any]]]] = []
        self.message_id = _MESSAGE_ID
        self.prompt_error: BaseException | None = None

    def subscribe_session_notifications(self, session_id: str) -> _ScriptedSubscription:
        self.ops.append(("subscribe", session_id))
        if not self._scripts:
            raise AssertionError("no scripted turn left for a new subscription")
        subscription = _ScriptedSubscription(
            self._scripts.pop(0), on_drain=self._on_drain
        )
        self.subscriptions.append(subscription)
        return subscription

    def session_prompt(
        self,
        session_id: str,
        content_blocks: list[dict[str, Any]],
        *,
        on_notification: object = None,
        notification_subscription: object = None,
    ) -> str:
        del on_notification, notification_subscription
        self.ops.append(("prompt", session_id))
        self.prompt_calls.append((session_id, content_blocks))
        if self.prompt_error is not None:
            raise self.prompt_error
        return self.message_id

    def close(self) -> None:
        self.closed = True


def _client(harness_client: _FakeHarnessClient) -> DshClient:
    return DshClient(harness=harness_client)


def test_stream_turn_timing_receipt_guard_and_idle_termination() -> None:
    """时序 + 守卫 + 终止：subscribe 先于 prompt；receipt 前通知（含
    messageId 不匹配者）不外发；事件按序 yield，idle 后终止且 idle 仍
    作为最后一条外发；正常 return 后订阅关闭。"""
    harness_client = _FakeHarnessClient(
        [
            # 上一轮尾巴（若守卫失效，旧 idle 会让本轮提前终止）
            _turn_end(_SESSION_ID, seq=90, turn=0),
            _status(_SESSION_ID, "idle"),
            # 无关 receipt：inserted 携带别的 messageId，不开闸
            _receipt(_SESSION_ID, "msg-other"),
            *_full_turn_script(_SESSION_ID, _MESSAGE_ID),
        ]
    )
    client = _client(harness_client)

    events = list(client.stream_turn(session_key=_SESSION_KEY, message="1+1?"))

    # 未 create_session 也能运行：session id 现场派生（":" → "-"）
    assert harness_client.ops == [
        ("subscribe", _SESSION_ID),
        ("prompt", _SESSION_ID),
    ]
    assert harness_client.prompt_calls == [
        (_SESSION_ID, [{"type": "text", "text": "1+1?"}])
    ]

    assert [e["method"] for e in events] == [
        "session.status",  # running
        "session.event",  # turn/start
        "session.event",  # assistant/chunk
        "session.event",  # assistant/message
        "session.event",  # turn/end
        "session.status",  # idle（终止判定，仍作为最后一条外发）
    ]
    assert _event_types(events) == [
        "turn/start",
        "assistant/chunk",
        "assistant/message",
        "turn/end",
    ]
    # 守卫：尾巴事件与 receipt（含 messageId 不匹配者）均未外发
    assert "agent/inbox/spliced" not in _event_types(events)
    assert all(
        e["payload"]["event"]["data"]["turn"] == 1
        for e in events
        if e["method"] == "session.event"
        and e["payload"]["event"]["type"] == "turn/end"
    )
    # 正常完成后订阅关闭
    assert harness_client.subscriptions[0].closed is True


def test_stream_turn_without_harness_raises_not_started() -> None:
    client = DshClient()

    with pytest.raises(DshClientError) as exc:
        list(client.stream_turn(session_key=_SESSION_KEY, message="q"))

    assert exc.value.reason == "not-started"


def test_abort_session_stops_active_stream_turn() -> None:
    """软 abort：置标志后消费循环在下一通知前 return，订阅随之关闭。"""
    harness_client = _FakeHarnessClient(_full_turn_script(_SESSION_ID, _MESSAGE_ID))
    client = _client(harness_client)

    gen = client.stream_turn(session_key=_SESSION_KEY, message="q")
    assert next(gen)["method"] == "session.status"  # running
    assert next(gen)["payload"]["event"]["type"] == "turn/start"

    client.abort_session(session_key=_SESSION_KEY)

    with pytest.raises(StopIteration):
        next(gen)

    subscription = harness_client.subscriptions[0]
    assert subscription.closed is True
    assert subscription.pending == 0
    assert subscription.drain_calls == 1


@pytest.mark.parametrize(
    "script",
    [
        [  # review-2①：receipt 迟迟不至，只有上一轮尾巴通知
            _session_event(
                _SESSION_ID,
                "turn/end",
                {"turn": 0, "reason": {"kind": "completed"}},
                seq=90,
            ),
            _status(_SESSION_ID, "idle"),
        ],
        [],  # review-2②：runtime 停发通知，一条都等不到
    ],
)
def test_abort_interrupts_stalled_guard_or_consumption(
    script: list[Notification | BaseException], monkeypatch: pytest.MonkeyPatch
) -> None:
    """review-2：通知停滞（receipt 迟迟不至 / runtime 停发）时软 abort 必须
    在一个轮询周期内打断消费循环，不能挂在无超时的 next() 上。"""
    monkeypatch.setattr(dsh_client_module, "_ABORT_POLL_INTERVAL_SECONDS", 0.01)
    polls = 0

    def _on_drain() -> None:
        nonlocal polls
        polls += 1
        if polls >= 3:
            client.abort_session(session_key=_SESSION_KEY)

    harness_client = _FakeHarnessClient(script, on_drain=_on_drain)
    client = _client(harness_client)

    gen = client.stream_turn(session_key=_SESSION_KEY, message="q")
    with pytest.raises(StopIteration):
        next(gen)

    subscription = harness_client.subscriptions[0]
    assert subscription.closed is True
    assert subscription.pending == 0
    assert subscription.drain_calls == 3


def test_stream_turn_after_abort_allows_new_turn() -> None:
    """spike-5：软 abort 后同 session 可继续提交新消息（标志在新 turn 清除）。"""
    harness_client = _FakeHarnessClient(
        _full_turn_script(_SESSION_ID, _MESSAGE_ID),
        _full_turn_script(_SESSION_ID, _MESSAGE_ID),
    )
    client = _client(harness_client)

    gen = client.stream_turn(session_key=_SESSION_KEY, message="q1")
    next(gen)
    client.abort_session(session_key=_SESSION_KEY)
    with pytest.raises(StopIteration):
        next(gen)

    events = list(client.stream_turn(session_key=_SESSION_KEY, message="q2"))

    assert _event_types(events) == [
        "turn/start",
        "assistant/chunk",
        "assistant/message",
        "turn/end",
    ]
    assert harness_client.prompt_calls[1] == (
        _SESSION_ID,
        [{"type": "text", "text": "q2"}],
    )
    assert len(harness_client.subscriptions) == 2
    assert all(sub.closed for sub in harness_client.subscriptions)


def test_abandoned_generator_closes_subscription() -> None:
    """spike-5b：生成器被遗弃（GeneratorExit）时订阅也必须显式 close。"""
    harness_client = _FakeHarnessClient(_full_turn_script(_SESSION_ID, _MESSAGE_ID))
    client = _client(harness_client)

    gen = client.stream_turn(session_key=_SESSION_KEY, message="q")
    next(gen)  # 消费一条，生成器悬挂在 yield 处
    gen.close()  # 模拟 TaskPool 停止后遗弃生成器

    assert harness_client.subscriptions[0].closed is True


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TransportClosedError("runtime exited"), "transport-closed"),
        (TimeoutError("next timed out"), "timeout"),
        (SdkProtocolError("malformed notification"), "protocol-error"),
    ],
)
def test_stream_turn_maps_subscription_errors(
    error: BaseException, reason: str
) -> None:
    harness_client = _FakeHarnessClient([_receipt(_SESSION_ID, _MESSAGE_ID), error])
    client = _client(harness_client)

    with pytest.raises(DshClientError) as exc:
        list(client.stream_turn(session_key=_SESSION_KEY, message="q"))

    assert exc.value.reason == reason
    assert harness_client.subscriptions[0].closed is True


def test_stream_turn_maps_prompt_json_rpc_error() -> None:
    harness_client = _FakeHarnessClient([])
    harness_client.prompt_error = JsonRpcError(-32000, "session prompt rejected")
    client = _client(harness_client)

    with pytest.raises(DshClientError) as exc:
        list(client.stream_turn(session_key=_SESSION_KEY, message="q"))

    assert exc.value.reason == "json-rpc-error"
    assert harness_client.subscriptions[0].closed is True  # finally 兜底


def test_list_agents_synthesizes_single_agent() -> None:
    payload = DshClient().list_agents()

    assert payload["defaultId"] == "main"
    assert payload["agents"] == [{"id": "main", "name": "dsh", "default": True}]


@pytest.mark.parametrize(
    ("method", "kwargs"),
    [
        ("list_sessions", {"agent_id": "main"}),
        ("get_agent", {"agent_id": "main"}),
        ("get_skills_status", {"agent_id": "main"}),
        ("answer_question", {"request_id": "q", "answers": [["a"]]}),
        ("reject_question", {"request_id": "q"}),
    ],
)
def test_unsupported_methods_raise_not_implemented(
    method: str, kwargs: dict[str, Any]
) -> None:
    with pytest.raises(NotImplementedError):
        getattr(DshClient(), method)(**kwargs)


def test_create_session_records_derived_mapping_without_prompt() -> None:
    harness_client = _FakeHarnessClient()
    client = _client(harness_client)

    client.create_session(session_key="agent:7:session:42")

    assert client._session_map == {"agent:7:session:42": "agent-7-session-42"}
    assert harness_client.ops == []
    assert harness_client.prompt_calls == []


def test_delete_session_removes_files_and_mapping(tmp_path: Path) -> None:
    """删除本 session 落盘文件；review-1：精确边界匹配，不误删 90/99
    数字前缀碰撞 session 的数据。"""
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    for name in (
        f"{_SESSION_ID}.jsonl",
        "agent-1-session-90.jsonl",
        "agent-1-session-99.meta.json",
    ):
        (session_root / name).write_text("{}", encoding="utf-8")
    for name in (_SESSION_ID, "agent-1-session-90"):
        nested = session_root / name
        nested.mkdir()
        (nested / "session.jsonl").write_text("{}", encoding="utf-8")
    unrelated = session_root / "other-session.jsonl"
    unrelated.write_text("{}", encoding="utf-8")

    harness_client = _FakeHarnessClient()
    client = _client(harness_client)
    client.update_config(session_root=str(session_root))
    client.create_session(session_key=_SESSION_KEY)

    client.delete_session(session_key=_SESSION_KEY)

    assert not (session_root / f"{_SESSION_ID}.jsonl").exists()
    assert not (session_root / _SESSION_ID).exists()
    assert (session_root / "agent-1-session-90.jsonl").exists()
    assert (session_root / "agent-1-session-90").exists()
    assert (session_root / "agent-1-session-99.meta.json").exists()
    assert unrelated.exists()
    assert _SESSION_KEY not in client._session_map


def test_delete_session_without_session_root_only_clears_mapping() -> None:
    harness_client = _FakeHarnessClient()
    client = _client(harness_client)
    client.create_session(session_key=_SESSION_KEY)

    client.delete_session(session_key=_SESSION_KEY)

    assert _SESSION_KEY not in client._session_map


@pytest.mark.parametrize(
    ("update", "expect_detach"),
    [
        ({"model": "deepseek-v4-pro"}, True),
        ({}, False),
        ({"model": "deepseek-v4-flash"}, False),  # 与默认值相同：无变更
    ],
)
def test_update_config_detaches_harness_only_on_change(
    update: dict[str, Any], expect_detach: bool
) -> None:
    harness_client = _FakeHarnessClient()
    client = DshClient(harness=harness_client)

    client.update_config(**update)

    assert harness_client.closed is expect_detach
    assert client.harness is (None if expect_detach else harness_client)


def test_update_config_detach_keeps_inflight_turn_alive() -> None:
    """detach 语义：不关在途 turn 的 harness，生成器退出（finally 释放引用）后才关闭。"""
    harness_client = _FakeHarnessClient(_full_turn_script(_SESSION_ID, _MESSAGE_ID))
    client = DshClient(harness=harness_client)

    gen = client.stream_turn(session_key=_SESSION_KEY, message="q")
    assert next(gen)["method"] == "session.status"  # 悬挂在 yield 处

    client.update_config(model="deepseek-v4-pro")

    assert client.harness is None
    assert harness_client.closed is False

    events = list(gen)  # 消费到 idle → finally 释放引用并关闭待回收 harness
    assert events[-1]["payload"]["status"] == "idle"
    assert harness_client.closed is True


def test_update_config_detach_then_ensure_harness_rebuilds() -> None:
    """detach 后 ensure_harness 按新配置重建（新实例，旧实例已关闭）。"""
    harness_client = _FakeHarnessClient()
    client = DshClient(harness=harness_client)

    client.update_config(model="deepseek-v4-pro")

    assert harness_client.closed is True
    harness = client.ensure_harness()
    assert isinstance(harness, DeepSeekHarness)
    assert harness is not harness_client
    assert harness.config.model == "deepseek-v4-pro"


def test_ensure_harness_builds_from_config_and_reuses_instance() -> None:
    client = DshClient()
    client.update_config(
        workspace_dir="/tmp/dsh-ws",
        session_root="/tmp/dsh-sr",
        provider="custom-endpoint",
        model="custom-model",
        api_key="sk-test",
        base_url="https://example.internal/v1",
        max_tokens=4096,
    )

    harness = client.ensure_harness()

    assert isinstance(harness, DeepSeekHarness)
    assert harness.config.cwd == "/tmp/dsh-ws"
    assert harness.config.session_root == "/tmp/dsh-sr"
    assert harness.config.provider == "custom-endpoint"
    assert harness.config.model == "custom-model"
    assert harness.config.api_key == "sk-test"
    assert harness.config.base_url == "https://example.internal/v1"
    assert harness.config.max_tokens == 4096
    assert client.ensure_harness() is harness
