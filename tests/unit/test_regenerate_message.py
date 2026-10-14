"""「重新生成」回归测试：不写用户消息、旧回复被替换（含事件解绑）、送给运行时的是原始提问。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from witty_service.adapter.websocket_client_pool import WebSocketClientPool
from witty_service.application import agent_manager as agent_manager_module
from witty_service.application.agent_manager import AgentManager
from witty_service.application.session_manager import SessionManager
from witty_service.domain.enums import AgentStatus
from witty_service.domain.errors import DomainError
from witty_service.persistence.db import (
    create_session_factory,
    create_sqlite_engine,
    init_db,
)
from witty_service.persistence.orm import (
    MessageEventORM,
    MessageORM,
    MessageStatus,
)
from witty_service.persistence.repositories import SqliteRepository
from witty_service.storage.workspace_store import LocalWorkspaceStore

AGENT_ID = "agent-regenerate"
SESSION_ID = "session-regenerate"


class _SandboxBackend:
    sandbox_type = "local_process"

    def start(self, **_: Any):  # pragma: no cover - 本测试不会拉起沙箱
        raise AssertionError("sandbox must not be started")

    def stop(self, *_: Any, **__: Any) -> None: ...

    def cleanup(self, *_: Any, **__: Any) -> None: ...


class _ScriptedWsClient:
    """按脚本回放事件的一次性 ws 客户端。"""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.is_connected = False
        self.sent: list[dict[str, Any]] = []
        self._events = events

    async def connect(self, session_id: str) -> None:
        self.is_connected = True

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def recv(self):
        async def _gen():
            for event in self._events:
                yield event

        return _gen()

    async def close(self) -> None:
        self.is_connected = False


def _event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": event_type,
        "session_id": SESSION_ID,
        "runtime_type": "opencode",
        "event_id": f"evt-{event_type}",
        "ts_ms": 1700000000000,
        "payload": payload,
    }


@pytest.fixture(autouse=True)
def _reset_stream_registry():
    """``_stream_registry`` 是模块级全局态，用例之间必须清理。"""
    agent_manager_module._stream_registry.cleanup(SESSION_ID)
    yield
    agent_manager_module._stream_registry.cleanup(SESSION_ID)


def _make_manager(
    tmp_path: Path,
) -> tuple[AgentManager, SqliteRepository, WebSocketClientPool]:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'witty.sqlite3'}")
    init_db(engine, auto_create=True)
    repository = SqliteRepository(create_session_factory(engine))
    repository.create_agent_with_id(
        agent_id=AGENT_ID,
        name="regenerate",
        sandbox_type="local_process",
        adapter_type="opencode",
        workspace_path=str(tmp_path / "workspace"),
        idle_timeout_seconds=300,
        status=AgentStatus.running,
    )
    repository.save_sandbox_state(
        AGENT_ID,
        sandbox_payload_json={
            "sandbox_id": "sandbox-regenerate",
            "agent_id": AGENT_ID,
            "workspace_path": str(tmp_path / "workspace"),
            "metadata": {},
        },
        adapter_base_url="http://127.0.0.1:1",
        adapter_ready=True,
    )
    repository.upsert_session(
        session_id=SESSION_ID,
        agent_id=AGENT_ID,
        status="idle",
        runtime_type="opencode",
        remote_runtime_agent_id="runtime-agent",
    )
    pool = WebSocketClientPool()
    manager = AgentManager(
        repository=repository,
        session_manager=SessionManager(repository),
        workspace_store=LocalWorkspaceStore(base_path=str(tmp_path / "workspace")),
        sandbox_backend=_SandboxBackend(),
        ws_client_pool=pool,
    )
    return manager, repository, pool


def _seed_completed_exchange(repository: SqliteRepository) -> tuple[str, str]:
    """写入一轮已完成的对话：[原始提问][旧回答]，旧回答带一条已落库事件。"""
    user_id = repository.create_message(
        agent_id=AGENT_ID, session_id=SESSION_ID, role="user", content="原始提问"
    )
    assistant_id = repository.create_message(
        agent_id=AGENT_ID,
        session_id=SESSION_ID,
        role="assistant",
        content="旧回答",
        status=MessageStatus.completed,
    )
    repository.create_message_events_bulk(
        agent_id=AGENT_ID,
        session_id=SESSION_ID,
        events=[(1, "message.delta", {"delta": "旧回答"})],
        message_id=assistant_id,
    )
    return user_id, assistant_id


def _all_messages(repository: SqliteRepository) -> list[MessageORM]:
    with repository._session_factory() as db:
        rows = (
            db.query(MessageORM)
            .filter(MessageORM.session_id == SESSION_ID)
            .order_by(MessageORM.created_at.asc())
            .all()
        )
        for row in rows:
            db.expunge(row)
        return rows


def _message_exists(repository: SqliteRepository, message_id: str) -> bool:
    with repository._session_factory() as db:
        return db.get(MessageORM, message_id) is not None


def _all_events(repository: SqliteRepository) -> list[MessageEventORM]:
    with repository._session_factory() as db:
        rows = (
            db.query(MessageEventORM)
            .filter(MessageEventORM.session_id == SESSION_ID)
            .order_by(MessageEventORM.seq_no.asc())
            .all()
        )
        for row in rows:
            db.expunge(row)
        return rows


async def _run_regenerate(
    manager: AgentManager,
    pool: WebSocketClientPool,
    client: _ScriptedWsClient,
) -> list[dict[str, Any]]:
    with patch.object(pool, "get_client", return_value=client):
        return [
            chunk["event"]
            async for chunk in manager.regenerate_message_stream(AGENT_ID, SESSION_ID)
        ]


def test_regenerate_replaces_last_assistant_without_persisting_user_message(
    tmp_path: Path,
) -> None:
    """核心回归：不写用户消息、旧回复被替换、送给运行时的是原始提问。"""

    async def run() -> None:
        manager, repository, pool = _make_manager(tmp_path)
        user_id, old_assistant_id = _seed_completed_exchange(repository)
        # 标题已按首条用户消息生成过；重生成不得覆盖它
        repository.update_session_metadata(SESSION_ID, title="原始提问")

        client = _ScriptedWsClient(
            [
                _event("message.delta", {"delta": "新"}),
                _event("message.completed", {"text": "新回答"}),
            ]
        )
        received = await _run_regenerate(manager, pool, client)

        assert received[-1]["type"] == "message.completed"

        # 1. 送给运行时的是原始提问，而不是 /regenerate 指令
        assert [message["payload"]["message"] for message in client.sent] == [
            "原始提问"
        ]
        assert client.sent[0]["type"] == "message.create"

        rows = _all_messages(repository)
        # 2. 没有多出 /regenerate 用户气泡
        assert [row.content for row in rows if row.role == "user"] == ["原始提问"]
        assert not any("/regenerate" in row.content for row in rows)

        # 3. 旧回复被替换：会话里只剩一条助手回复，且是新内容
        assistant_rows = [row for row in rows if row.role == "assistant"]
        assert len(assistant_rows) == 1
        new_assistant_id = assistant_rows[0].id
        assert new_assistant_id != old_assistant_id
        assert assistant_rows[0].content == "新回答"
        assert assistant_rows[0].status == MessageStatus.completed

        # 4. 旧回复的事件随消息一起删除，不留孤儿；新回复的事件挂在它自己名下
        events = _all_events(repository)
        assert events
        assert all(event.message_id == new_assistant_id for event in events)

        # 5. 历史投影只返回 [提问][新回答]，与刷新后看到的一致
        messages, has_more = repository.get_messages_with_events(SESSION_ID)
        assert has_more is False
        assert [(item["role"], item["id"]) for item in messages] == [
            ("user", user_id),
            ("assistant", new_assistant_id),
        ]
        # 新回复的事件仍完整挂在它自己名下（seq_no 与旧事件不冲突）
        assert [event["type"] for event in messages[1]["events"]].count(
            "message.delta"
        ) == 1

        # 6. 会话标题不受重生成影响（仍由首条用户消息决定）
        session = repository.get_session(SESSION_ID)
        assert session is not None
        assert session.title == "原始提问"

    asyncio.run(run())


def test_regenerate_rejects_while_session_stream_is_active(tmp_path: Path) -> None:
    """同会话已有生成在飞时拒绝重生成（409），且不改动任何数据。"""

    async def run() -> None:
        manager, repository, _pool = _make_manager(tmp_path)
        _seed_completed_exchange(repository)

        agent_manager_module._stream_registry.start_stream(SESSION_ID)
        try:
            with pytest.raises(DomainError) as excinfo:
                async for _ in manager.regenerate_message_stream(AGENT_ID, SESSION_ID):
                    pass
            assert excinfo.value.code == "SESSION_BUSY"
            assert excinfo.value.status_code == 409
            assert len(_all_messages(repository)) == 2
        finally:
            agent_manager_module._stream_registry.end_stream(SESSION_ID)

    asyncio.run(run())


def test_regenerate_without_assistant_message_returns_not_found(
    tmp_path: Path,
) -> None:
    """会话里还没有助手回复：无从重生成，404（不是 500）。"""

    async def run() -> None:
        manager, repository, _pool = _make_manager(tmp_path)
        repository.create_message(
            agent_id=AGENT_ID, session_id=SESSION_ID, role="user", content="只有提问"
        )

        with pytest.raises(DomainError) as excinfo:
            async for _ in manager.regenerate_message_stream(AGENT_ID, SESSION_ID):
                pass
        assert excinfo.value.code == "REGENERATE_TARGET_NOT_FOUND"
        assert excinfo.value.status_code == 404
        # 拒绝时不能顺手删掉那条用户消息
        assert [row.content for row in _all_messages(repository)] == ["只有提问"]

    asyncio.run(run())


def test_regenerate_uses_prompt_preceding_the_target_assistant_message(
    tmp_path: Path,
) -> None:
    """目标助手消息之后还有更新的用户提问时，仍要用当初触发它的那个提问。"""

    async def run() -> None:
        manager, repository, pool = _make_manager(tmp_path)
        repository.create_message(
            agent_id=AGENT_ID, session_id=SESSION_ID, role="user", content="第一问"
        )
        repository.create_message(
            agent_id=AGENT_ID,
            session_id=SESSION_ID,
            role="assistant",
            content="第一答",
            status=MessageStatus.completed,
        )
        # 之后用户又问了第二句，但那一轮失败了、没有留下助手回复
        repository.create_message(
            agent_id=AGENT_ID, session_id=SESSION_ID, role="user", content="第二问"
        )

        client = _ScriptedWsClient([_event("message.completed", {"text": "重答"})])
        await _run_regenerate(manager, pool, client)

        assert [message["payload"]["message"] for message in client.sent] == ["第一问"]

    asyncio.run(run())


def test_regenerate_prepends_interruption_prefix_from_the_replaced_message(
    tmp_path: Path,
) -> None:
    """被替换的回复是「已中断」时，前缀要按**删行前**的状态判定。"""

    async def run() -> None:
        manager, repository, pool = _make_manager(tmp_path)
        repository.create_message(
            agent_id=AGENT_ID, session_id=SESSION_ID, role="user", content="原始提问"
        )
        repository.create_message(
            agent_id=AGENT_ID,
            session_id=SESSION_ID,
            role="assistant",
            content="半截回答",
            status=MessageStatus.interrupted,
        )

        client = _ScriptedWsClient([_event("message.completed", {"text": "重答"})])
        await _run_regenerate(manager, pool, client)

        sent = client.sent[0]["payload"]["message"]
        assert sent.endswith("原始提问")
        assert sent != "原始提问"  # 前缀生效

    asyncio.run(run())


async def _drain_stream(gen: Any) -> None:
    """把流跑到结束（失败也算），用于并发用例里观察副作用。"""
    try:
        async for _ in gen:
            pass
    except Exception:
        pass


def test_regenerate_keeps_old_answer_when_ws_handshake_fails(tmp_path: Path) -> None:
    """写出第一个事件前失败（adaptor 不可达等）：旧答与其事件必须原样保留。"""

    async def run() -> None:
        manager, repository, _pool = _make_manager(tmp_path)
        _user_id, old_assistant_id = _seed_completed_exchange(repository)

        with (
            patch.object(
                manager,
                "_prepare_ws_message_client",
                new=AsyncMock(side_effect=RuntimeError("adaptor unreachable")),
            ),
            pytest.raises(RuntimeError),
        ):
            async for _ in manager.regenerate_message_stream(AGENT_ID, SESSION_ID):
                pass

        assert [row.content for row in _all_messages(repository)] == [
            "原始提问",
            "旧回答",
        ]
        assert [event.message_id for event in _all_events(repository)] == [
            old_assistant_id
        ]
        # 预约必须释放，否则该会话会一直被判为「生成中」
        assert agent_manager_module._stream_registry.is_active(SESSION_ID) is False

    asyncio.run(run())


def test_regenerate_rejects_second_turn_during_handshake(tmp_path: Path) -> None:
    """握手（建流前的网络往返）期间发起的第二个重跑必须被 409 拒绝。"""

    async def run() -> None:
        manager, repository, _pool = _make_manager(tmp_path)
        _seed_completed_exchange(repository)

        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocking_prepare(*_args: Any, **_kwargs: Any) -> Any:
            entered.set()
            await release.wait()
            raise RuntimeError("handshake aborted")

        with patch.object(manager, "_prepare_ws_message_client", new=blocking_prepare):
            first = asyncio.create_task(
                _drain_stream(manager.regenerate_message_stream(AGENT_ID, SESSION_ID))
            )
            await asyncio.wait_for(entered.wait(), timeout=5)

            with pytest.raises(DomainError) as excinfo:
                async for _ in manager.regenerate_message_stream(AGENT_ID, SESSION_ID):
                    pass
            assert excinfo.value.code == "SESSION_BUSY"
            assert excinfo.value.status_code == 409

            release.set()
            await asyncio.wait_for(first, timeout=5)

        # 两个请求都没能在写第一个事件之前删掉旧答
        assert [row.content for row in _all_messages(repository)] == [
            "原始提问",
            "旧回答",
        ]

    asyncio.run(run())


def test_delete_message_is_scoped_to_session_and_idempotent(tmp_path: Path) -> None:
    """仓储层硬删：跨会话不误删，重复删除返回 False。"""

    _manager, repository, _pool = _make_manager(tmp_path)

    other_session_id = "session-other"
    repository.upsert_session(
        session_id=other_session_id,
        agent_id=AGENT_ID,
        status="idle",
    )
    target_id = repository.create_message(
        agent_id=AGENT_ID, session_id=SESSION_ID, role="assistant", content="待删"
    )
    other_id = repository.create_message(
        agent_id=AGENT_ID,
        session_id=other_session_id,
        role="assistant",
        content="别删我",
    )

    # 会话不匹配：不删
    assert repository.delete_message(other_id, session_id=SESSION_ID) is False
    assert _message_exists(repository, other_id) is True
    assert _message_exists(repository, target_id) is True

    assert repository.delete_message(target_id, session_id=SESSION_ID) is True
    assert _message_exists(repository, target_id) is False
    # 跨会话的那条不受影响
    assert _message_exists(repository, other_id) is True
    # 幂等
    assert repository.delete_message(target_id, session_id=SESSION_ID) is False
