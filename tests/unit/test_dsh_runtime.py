from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from witty_agent_server.infra.clients.base import ClientBase
from witty_agent_server.infra.clients.dsh_client import DshClientError
from witty_agent_server.runtimes.dsh_runtime import DshRuntime
from witty_agent_server.runtimes.runtime_base import RuntimeTurnEvent

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "dsh"

_TEXT_ONLY_SESSION = "minimal-001"
_TOOLS_SESSION = "turn-with-tools-001"
_ERROR_SESSION = "spike-error"

_TOOL_CALL_ID = "call_00_fZ3qACLx81jiTU9f51Ia4236"
_TOOL_NAME = "bash"
_TOOL_ARGUMENTS = {
    "command": "echo hello-from-dsh && pwd && ls -la",
    "description": "Echo string, print workdir, list files",
}


# =============================================================================
# 辅助：fixture 加载 / 手造 notification / 假 client
# =============================================================================


def _load_fixture(name: str) -> list[dict[str, Any]]:
    """逐行加载 P0 实测 notification 样本（DshClient.stream_turn 的 yield 形状）。"""
    path = _FIXTURES_DIR / f"{name}.notifications.jsonl"
    notifications: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                notifications.append(json.loads(line))
    return notifications


def _notification(
    session_id: str,
    event_type: str,
    data: dict[str, Any] | None = None,
    *,
    method: str = "session.event",
) -> dict[str, Any]:
    payload: dict[str, Any] = {"sessionId": session_id}
    if method == "session.event":
        payload["event"] = {
            "type": event_type,
            "seq": 0,
            "time": 0,
            "data": data or {},
        }
    else:
        payload.update(data or {})
    return {"method": method, "payload": payload}


def _chunk(session_id: str, chunk: dict[str, Any]) -> dict[str, Any]:
    return _notification(
        session_id,
        "assistant/chunk",
        {"turn": 1, "step": 1, "chunk": chunk},
    )


def _replay(
    notifications: list[dict[str, Any]],
    *,
    session_id: str | None,
) -> tuple[list[RuntimeTurnEvent], dict[str, str]]:
    """按 DshClient.stream_turn 顺序回放原始 notification，收集统一事件。"""
    tool_names: dict[str, str] = {}
    events: list[RuntimeTurnEvent] = [
        event
        for raw in notifications
        for event in DshRuntime._map_dsh_event(
            raw, session_id=session_id, tool_names_by_call_id=tool_names
        )
    ]
    return events, tool_names


def _event_types(events: list[RuntimeTurnEvent]) -> list[str]:
    return [event["type"] for event in events]


def _of_type(events: list[RuntimeTurnEvent], event_type: str) -> list[RuntimeTurnEvent]:
    return [event for event in events if event["type"] == event_type]


class _ReplayClient(ClientBase):
    """回放固定 notification 列表的假 client（构造注入，仓库惯例不用 patch）。"""

    def __init__(
        self,
        notifications: list[dict[str, Any]] | None = None,
        *,
        error: DshClientError | None = None,
    ) -> None:
        self._notifications = list(notifications or [])
        self._error = error

    def list_agents(self) -> dict[str, Any]:
        return {"defaultId": "main", "agents": []}

    def list_sessions(self, *, agent_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_agent(self, *, agent_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_skills_status(self, *, agent_id: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def create_session(self, *, session_key: str) -> None:
        return None

    def delete_session(self, *, session_key: str) -> None:
        return None

    def abort_session(self, *, session_key: str) -> None:
        return None

    def stream_turn(
        self, *, session_key: str, message: str
    ) -> Iterator[dict[str, Any]]:
        del session_key, message
        yield from self._notifications
        if self._error is not None:
            raise self._error


def _run_turn_with(client: ClientBase, *, session_key: str) -> list[RuntimeTurnEvent]:
    runtime = DshRuntime(client=client)
    return list(runtime.run_turn(session_key=session_key, message="ping"))


# =============================================================================
# _map_dsh_event — 单事件直测
# =============================================================================


def test_runtime_type_is_dsh() -> None:
    assert DshRuntime(client=None).runtime_type == "dsh"


def test_turn_start_maps_to_message_started() -> None:
    raw = _notification("s1", "turn/start", {"turn": 3})

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == [{"type": "message.started", "payload": {"turn": 3}}]


def test_text_delta_maps_to_message_delta() -> None:
    raw = _chunk("s1", {"type": "text-delta", "index": 1, "text": "你好"})

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == [{"type": "message.delta", "payload": {"delta": "你好"}}]


def test_reasoning_delta_maps_to_thinking_delta() -> None:
    raw = _chunk("s1", {"type": "reasoning-delta", "index": 0, "text": "The user"})

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == [{"type": "thinking.delta", "payload": {"delta": "The user"}}]


def test_empty_text_delta_yields_nothing() -> None:
    raw = _chunk("s1", {"type": "text-delta", "index": 1, "text": ""})

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == []


def test_reasoning_block_end_maps_to_thinking() -> None:
    raw = _chunk(
        "s1",
        {
            "type": "block-end",
            "index": 0,
            "block": {"type": "reasoning", "text": "推理"},
        },
    )

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == [{"type": "thinking", "payload": {"thinking": "推理"}}]


@pytest.mark.parametrize(
    "chunk",
    [
        # text / tool-call 的 block-end 不透出：文本走 delta 流、工具走 tool/call 终值
        {"type": "block-end", "index": 1, "block": {"type": "text", "text": "全文"}},
        {
            "type": "block-end",
            "index": 2,
            "block": {
                "type": "tool-call",
                "id": "call-1",
                "name": "bash",
                "arguments": "{}",
            },
        },
        # block-start / tool-call-delta / usage / finish 子类型不透出
        {"type": "block-start", "index": 0, "blockType": "reasoning"},
        {"type": "tool-call-delta", "index": 2, "id": "call-1", "argumentsDelta": '{"'},
        {
            "type": "usage",
            "usage": {
                "inputTokens": 1,
                "outputTokens": 1,
                "cacheReadTokens": 0,
                "reasoningTokens": 0,
            },
        },
        {"type": "finish", "reason": {"kind": "stop"}},
    ],
)
def test_non_payload_chunk_subtypes_yield_nothing(chunk: dict[str, Any]) -> None:
    raw = _chunk("s1", chunk)

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == []


def test_assistant_message_final_yields_usage_and_completed() -> None:
    raw = _notification(
        "s1",
        "assistant/message",
        {
            "turn": 1,
            "step": 1,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "reasoning", "text": "推理"},
                    {"type": "text", "text": "答案"},
                ],
            },
            "usage": {
                "inputTokens": 10,
                "outputTokens": 5,
                "cacheReadTokens": 0,
                "reasoningTokens": 2,
            },
        },
    )

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == [
        {
            "type": "session.usage",
            "payload": {
                "usage": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "cacheReadTokens": 0,
                    "reasoningTokens": 2,
                }
            },
        },
        {"type": "message.completed", "payload": {"text": "答案"}},
    ]


def test_assistant_message_intermediate_step_skips_completed() -> None:
    """中间步（finish=tool-calls）含 tool-call 块：轮未结束不产出 completed。"""
    raw = _notification(
        "s1",
        "assistant/message",
        {
            "turn": 1,
            "step": 1,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "先执行命令"},
                    {"type": "tool-call", "id": "call-1", "name": "bash"},
                ],
            },
            "usage": {
                "inputTokens": 10,
                "outputTokens": 5,
                "cacheReadTokens": 0,
                "reasoningTokens": 2,
            },
        },
    )

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert _event_types(events) == ["session.usage"]


def test_assistant_message_without_usage_yields_completed_only() -> None:
    raw = _notification(
        "s1",
        "assistant/message",
        {
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "答案"}],
            }
        },
    )

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == [{"type": "message.completed", "payload": {"text": "答案"}}]


def test_tool_call_maps_to_started_with_parsed_arguments() -> None:
    raw = _notification(
        "s1",
        "tool/call",
        {
            "turn": 1,
            "step": 1,
            "callId": "call-1",
            "name": "bash",
            "arguments": '{"command": "echo hi", "description": "echo"}',
        },
    )
    tool_names: dict[str, str] = {}

    events = list(
        DshRuntime._map_dsh_event(
            raw, session_id="s1", tool_names_by_call_id=tool_names
        )
    )

    assert events == [
        {
            "type": "tool.call.started",
            "payload": {
                "stage": "started",
                "tool_name": "bash",
                "tool_call_id": "call-1",
                "arguments": {"command": "echo hi", "description": "echo"},
            },
        }
    ]
    assert tool_names == {"call-1": "bash"}


def test_tool_call_invalid_json_arguments_kept_raw() -> None:
    raw = _notification(
        "s1",
        "tool/call",
        {"callId": "call-1", "name": "bash", "arguments": '{"broken'},
    )

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events[0]["payload"]["arguments"] == '{"broken'


def test_tool_call_missing_call_id_yields_nothing() -> None:
    raw = _notification("s1", "tool/call", {"name": "bash", "arguments": "{}"})

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == []


def test_tool_result_maps_to_response() -> None:
    raw = _notification(
        "s1",
        "tool/result",
        {
            "turn": 1,
            "step": 1,
            "message": {
                "source": {"kind": "tool", "callId": "call-1"},
                "content": [
                    {
                        "type": "tool-result",
                        "toolCallId": "call-1",
                        "content": [{"type": "text", "text": "hello\nworld"}],
                        "isError": False,
                    }
                ],
                "role": "user",
            },
        },
    )

    events = list(
        DshRuntime._map_dsh_event(
            raw, session_id="s1", tool_names_by_call_id={"call-1": "bash"}
        )
    )

    assert events == [
        {
            "type": "tool.call.response",
            "payload": {
                "stage": "response",
                "name": "bash",
                "tool_call_id": "call-1",
                "content": "hello\nworld",
                "is_error": False,
            },
        }
    ]


def test_tool_result_without_prior_call_uses_unknown_name() -> None:
    raw = _notification(
        "s1",
        "tool/result",
        {
            "message": {
                "source": {"kind": "tool", "callId": "call-1"},
                "content": [
                    {
                        "type": "tool-result",
                        "content": [{"type": "text", "text": "boom"}],
                        "isError": True,
                    }
                ],
            }
        },
    )

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == [
        {
            "type": "tool.call.response",
            "payload": {
                "stage": "response",
                "name": "unknown",
                "tool_call_id": "call-1",
                "content": "boom",
                "is_error": True,
            },
        }
    ]


# ---------------------------------------------------------------------------
# write 工具 → artifact.* 事件
# ---------------------------------------------------------------------------

_WRITE_ARGS = '{"file_path": "output/demo.html", "content": "<h1>hi</h1>"}'


def _write_result_raw(*, is_error: bool = False) -> dict[str, Any]:
    return _notification(
        "s1",
        "tool/result",
        {
            "message": {
                "source": {"kind": "tool", "callId": "call-1"},
                "content": [
                    {
                        "type": "tool-result",
                        "content": [
                            {"type": "text", "text": "boom" if is_error else "written"}
                        ],
                        "isError": is_error,
                    }
                ],
            }
        },
    )


def test_tool_call_write_maps_to_started_with_arguments() -> None:
    events = list(
        DshRuntime._map_dsh_event(
            _notification(
                "s1",
                "tool/call",
                {"callId": "call-1", "name": "write", "arguments": _WRITE_ARGS},
            ),
            session_id="s1",
        )
    )
    # artifact.* 事件已上收到 RuntimeBase._on_artifact_event（见 test_runtime_artifact_event.py），
    # 静态映射层只负责标准 tool.call.*。
    assert [e["type"] for e in events] == ["tool.call.started"]
    started = events[0]["payload"]
    assert started["stage"] == "started"
    assert started["tool_name"] == "write"
    assert started["arguments"] == {
        "file_path": "output/demo.html",
        "content": "<h1>hi</h1>",
    }


def test_tool_result_write_maps_to_response_with_content() -> None:
    events = list(
        DshRuntime._map_dsh_event(
            _write_result_raw(),
            session_id="s1",
            tool_names_by_call_id={"call-1": "write"},
        )
    )
    assert [e["type"] for e in events] == ["tool.call.response"]
    response = events[0]["payload"]
    assert response["stage"] == "response"
    assert response["name"] == "write"
    assert response["content"] == "written"
    assert response["is_error"] is False


def test_tool_result_write_error_marks_response_error() -> None:
    events = list(
        DshRuntime._map_dsh_event(
            _write_result_raw(is_error=True),
            session_id="s1",
            tool_names_by_call_id={"call-1": "write"},
        )
    )
    response = events[0]["payload"]
    assert response["is_error"] is True
    assert response["content"] == "boom"


def test_turn_end_completed_maps_to_turn_completed_only() -> None:
    raw = _notification("s1", "turn/end", {"turn": 1, "reason": {"kind": "completed"}})

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == [{"type": "turn.completed", "payload": {"reason": "completed"}}]


def test_turn_end_error_yields_turn_completed_and_stream_error() -> None:
    raw = _notification(
        "s1",
        "turn/end",
        {
            "turn": 1,
            "reason": {
                "kind": "error",
                "error": {
                    "message": "model not found",
                    "code": "INVALID_REQUEST",
                    "status": 400,
                },
            },
        },
    )

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == [
        {
            "type": "stream.error",
            "payload": {
                "code": "INVALID_REQUEST",
                "message": "model not found",
                "status": 400,
            },
        },
        {"type": "turn.completed", "payload": {"reason": "error"}},
    ]


@pytest.mark.parametrize(
    "event_type",
    [
        "step/start",
        "step/end",
        "user/message",
        "session/title",
        "request/header",
        "request/context",
        "agent/inbox/spliced",
        "subagent.started",
        "subagent.finished",
    ],
)
def test_unmapped_event_types_yield_nothing(event_type: str) -> None:
    raw = _notification("s1", event_type, {"turn": 1})

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == []


def test_session_status_notification_yields_nothing() -> None:
    """轮终止判定（session.status idle）由 DshClient 承载，映射层不透出。"""
    raw = _notification("s1", "", {"status": "idle"}, method="session.status")

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == []


def test_foreign_session_events_filtered() -> None:
    """子 session（subagent 树）事件不透出（MVP 口径）。"""
    raw = _notification("s1-sub-1", "turn/start", {"turn": 1})

    events = list(DshRuntime._map_dsh_event(raw, session_id="s1"))

    assert events == []


def test_no_session_filter_when_session_id_is_none() -> None:
    raw = _notification("s1-sub-1", "turn/start", {"turn": 1})

    events = list(DshRuntime._map_dsh_event(raw))

    assert events == [{"type": "message.started", "payload": {"turn": 1}}]


# =============================================================================
# P0 实测样本回放 — _map_dsh_event 全流直测
# =============================================================================


def test_text_only_fixture_replay() -> None:
    notifications = _load_fixture("turn-text-only")

    events, tool_names = _replay(notifications, session_id=_TEXT_ONLY_SESSION)
    types = _event_types(events)

    assert types[0] == "message.started"
    assert types[-1] == "turn.completed"
    assert "stream.error" not in types

    completed = _of_type(events, "message.completed")
    assert len(completed) == 1
    assert completed[0]["payload"]["text"] == "2"

    deltas = _of_type(events, "message.delta")
    assert [event["payload"]["delta"] for event in deltas] == ["2"]

    thinking = _of_type(events, "thinking")
    assert len(thinking) == 1
    assert thinking[0]["payload"]["thinking"].startswith("The user asks")
    assert len(_of_type(events, "thinking.delta")) == 34

    usage_events = _of_type(events, "session.usage")
    assert usage_events == [
        {
            "type": "session.usage",
            "payload": {
                "usage": {
                    "inputTokens": 2171,
                    "outputTokens": 36,
                    "cacheReadTokens": 0,
                    "reasoningTokens": 34,
                }
            },
        }
    ]
    assert tool_names == {}


def test_tools_fixture_replay() -> None:
    notifications = _load_fixture("turn-with-tools")

    events, tool_names = _replay(notifications, session_id=_TOOLS_SESSION)
    types = _event_types(events)

    assert types[0] == "message.started"
    assert types[-1] == "turn.completed"
    assert "stream.error" not in types

    # 中间步（finish=tool-calls）不产出 message.completed：全轮仅终步一条。
    completed = _of_type(events, "message.completed")
    assert len(completed) == 1
    assert completed[0]["payload"]["text"].startswith("命令已成功执行")

    # assistant/message 每个 step 一条 → session.usage 两条（spike-2）。
    usage_events = _of_type(events, "session.usage")
    assert len(usage_events) == 2
    assert usage_events[0]["payload"]["usage"]["inputTokens"] == 2197
    assert usage_events[1]["payload"]["usage"]["cacheReadTokens"] == 2432

    # 工具事件：call → result，callId 一致，名称经 tool/call 记录回填。
    started = _of_type(events, "tool.call.started")
    responses = _of_type(events, "tool.call.response")
    assert len(started) == 1
    assert len(responses) == 1
    assert started[0]["payload"] == {
        "stage": "started",
        "tool_name": _TOOL_NAME,
        "tool_call_id": _TOOL_CALL_ID,
        "arguments": _TOOL_ARGUMENTS,
    }
    assert responses[0]["payload"]["name"] == _TOOL_NAME
    assert responses[0]["payload"]["tool_call_id"] == _TOOL_CALL_ID
    assert "hello-from-dsh" in responses[0]["payload"]["content"]
    assert responses[0]["payload"]["is_error"] is False
    assert tool_names == {_TOOL_CALL_ID: _TOOL_NAME}

    # 两个 step 各产出一条完整推理块（thinking）。
    assert len(_of_type(events, "thinking")) == 2


def test_error_fixture_replay() -> None:
    notifications = _load_fixture("turn-error")

    events, _tool_names = _replay(notifications, session_id=_ERROR_SESSION)
    types = _event_types(events)

    # error 轮无 assistant/message：静态映射不含 message.completed，
    # 错误先发、turn.completed 收尾。
    assert types == ["message.started", "stream.error", "turn.completed"]
    stream_error = events[1]
    assert stream_error["type"] == "stream.error"
    assert stream_error["payload"]["code"] == "INVALID_REQUEST"
    assert stream_error["payload"]["status"] == 400
    assert (
        "deepseek-v4-flash-model-does-not-exist-xyz"
        in (stream_error["payload"]["message"])
    )
    assert events[2]["payload"] == {"reason": "error"}


# =============================================================================
# run_turn 管线 — hooks / 兜底 / 异常转换 / 子 session 过滤
# =============================================================================


def test_run_turn_text_only_pipeline() -> None:
    client = _ReplayClient(_load_fixture("turn-text-only"))

    events = _run_turn_with(client, session_key=_TEXT_ONLY_SESSION)
    types = _event_types(events)

    assert types[0] == "message.started"
    assert types[-1] == "turn.completed"
    completed = _of_type(events, "message.completed")
    assert len(completed) == 1
    assert completed[0]["payload"]["text"] == "2"


def test_run_turn_tools_pipeline() -> None:
    client = _ReplayClient(_load_fixture("turn-with-tools"))

    events = _run_turn_with(client, session_key=_TOOLS_SESSION)
    types = _event_types(events)

    # run_turn 兜底去重后：started 单条、工具 started/completed 各一条、
    # message.completed 单条（终步）、turn.completed 收尾。
    assert types[0] == "message.started"
    assert types[-1] == "turn.completed"
    assert types.count("message.started") == 1
    assert types.count("message.completed") == 1
    assert types.count("tool.call.started") == 1
    assert types.count("tool.call.response") == 1


def test_run_turn_error_turn_no_synthesized_completed() -> None:
    """error 轮不伪造 message.completed：stream.error 先发、turn.completed 收尾。"""
    client = _ReplayClient(_load_fixture("turn-error"))

    events = _run_turn_with(client, session_key=_ERROR_SESSION)

    assert _event_types(events) == [
        "message.started",
        "stream.error",
        "turn.completed",
    ]
    assert events[1]["payload"]["code"] == "INVALID_REQUEST"
    assert events[2]["payload"] == {"reason": "error"}


def test_run_turn_completed_fallback_uses_accumulated_deltas() -> None:
    """无 assistant/message 的畸形轮：completed 兜底取累积 delta 文本。"""
    notifications = [
        _notification("s1", "turn/start", {"turn": 1}),
        _chunk("s1", {"type": "text-delta", "index": 1, "text": "AB"}),
        _chunk("s1", {"type": "text-delta", "index": 1, "text": "CD"}),
        _notification("s1", "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
    ]
    client = _ReplayClient(notifications)

    events = _run_turn_with(client, session_key="s1")

    assert _event_types(events) == [
        "message.started",
        "message.delta",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]
    assert events[3]["payload"] == {"text": "ABCD"}


def test_run_turn_synthesizes_message_started_when_missing() -> None:
    notifications = [
        _chunk("s1", {"type": "text-delta", "index": 1, "text": "x"}),
        _notification(
            "s1",
            "assistant/message",
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "x"}],
                }
            },
        ),
        _notification("s1", "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
    ]
    client = _ReplayClient(notifications)

    events = _run_turn_with(client, session_key="s1")

    assert _event_types(events) == [
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]
    assert events[0]["payload"] == {}


def test_run_turn_client_error_converted_to_stream_error() -> None:
    """DshClientError 不上抛，转为 stream.error 事件并以 turn.completed 收尾。"""
    error = DshClientError(reason="transport-closed", message="dsh stream_turn failed")
    notifications = [
        _notification("s1", "turn/start", {"turn": 1}),
        _chunk("s1", {"type": "text-delta", "index": 1, "text": "部分"}),
    ]
    client = _ReplayClient(notifications, error=error)

    events = _run_turn_with(client, session_key="s1")

    assert _event_types(events) == [
        "message.started",
        "message.delta",
        "stream.error",
        "turn.completed",
    ]
    assert events[2]["payload"] == {
        "code": "transport-closed",
        "message": "dsh stream_turn failed",
    }
    assert events[3]["payload"] == {"reason": "error"}


def test_run_turn_client_error_before_any_event_synthesizes_started() -> None:
    """异常发生在任何事件产出之前：错误流仍以 message.started 开头补齐。"""
    error = DshClientError(reason="not-started", message="harness is not started")
    client = _ReplayClient(notifications=[], error=error)

    events = _run_turn_with(client, session_key="s1")

    assert _event_types(events) == [
        "message.started",
        "stream.error",
        "turn.completed",
    ]
    assert events[0]["payload"] == {}
    assert events[1]["payload"] == {
        "code": "not-started",
        "message": "harness is not started",
    }
    assert events[2]["payload"] == {"reason": "error"}


def test_stream_message_terminates_on_error_turn() -> None:
    """error 轮经 stream_message 不悬挂：以 turn.completed 产出 done 终止。"""
    runtime = DshRuntime(client=_ReplayClient(_load_fixture("turn-error")))

    chunks = list(runtime.stream_message(session_id=_ERROR_SESSION, message="ping"))

    assert chunks == [{"type": "done"}]


def test_run_turn_filters_subsession_events() -> None:
    """session_key 派生 dsh session id（`:`→`-`），子 session 事件被过滤。"""
    notifications = [
        _notification("agent-1-session-9", "turn/start", {"turn": 1}),
        _chunk(
            "agent-1-session-9-sub-1", {"type": "text-delta", "index": 1, "text": "子"}
        ),
        _chunk("agent-1-session-9", {"type": "text-delta", "index": 1, "text": "父"}),
        _notification(
            "agent-1-session-9",
            "assistant/message",
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "父"}],
                }
            },
        ),
        _notification(
            "agent-1-session-9",
            "turn/end",
            {"turn": 1, "reason": {"kind": "completed"}},
        ),
    ]
    client = _ReplayClient(notifications)

    events = _run_turn_with(client, session_key="agent:1:session:9")

    deltas = _of_type(events, "message.delta")
    assert [event["payload"]["delta"] for event in deltas] == ["父"]
    assert _event_types(events) == [
        "message.started",
        "message.delta",
        "message.completed",
        "turn.completed",
    ]


def test_question_ops_not_supported() -> None:
    """dsh 协议不支持审批/提问：保持基类 NotImplementedError。"""
    runtime = DshRuntime(client=_ReplayClient())

    with pytest.raises(NotImplementedError):
        runtime.answer_question(request_id="q1", answers=[["a"]])
    with pytest.raises(NotImplementedError):
        runtime.reject_question(request_id="q1")
