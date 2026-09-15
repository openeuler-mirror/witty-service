"""流被异常中断时的收尾行为回归测试。

对应线上故障：消费 ws 期间事件循环被同步落库堵死，uvicorn 的 keepalive ping
超时把连接以 1011 掐断，``consume_ws`` 走到 except 分支后：
  - assistant 消息永远停在 ``generating``（前端重启后据此判定"生成中"）
  - session 永远停在 ``running``（同会话重新提问被 SESSION_BUSY 挡住）
  - 缓冲里的事件与最新正文丢失
这里断言修好之后的三件事：事件落库、正文落定、消息/会话终态。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import patch

from witty_service.adapter.exceptions import AdaptorReceiveError
from witty_service.adapter.websocket_client_pool import WebSocketClientPool
from witty_service.application.agent_manager import AgentManager
from witty_service.application.session_manager import SessionManager
from witty_service.domain.enums import AgentStatus
from witty_service.persistence.db import (
    create_session_factory,
    create_sqlite_engine,
    init_db,
)
from witty_service.persistence.orm import (
    MessageEventORM,
    MessageORM,
    MessageStatus,
    SessionORM,
)
from witty_service.persistence.repositories import SqliteRepository
from witty_service.storage.workspace_store import LocalWorkspaceStore

AGENT_ID = "agent-recovery"
SESSION_ID = "session-recovery"


class _SandboxBackend:
    sandbox_type = "local_process"

    def start(self, **_: Any):  # pragma: no cover - 本测试不会拉起沙箱
        raise AssertionError("sandbox must not be started")

    def stop(self, *_: Any, **__: Any) -> None: ...

    def cleanup(self, *_: Any, **__: Any) -> None: ...


class _ScriptedWsClient:
    """按脚本回放事件，随后抛出给定的异常（模拟 ws 被掐断）。"""

    def __init__(
        self,
        events: list[dict[str, Any]],
        error: Exception | None = None,
    ) -> None:
        self.is_connected = False
        self.sent: list[dict[str, Any]] = []
        self._events = events
        self._error = error

    async def connect(self, session_id: str) -> None:
        self.is_connected = True

    async def send(self, message: dict[str, Any]) -> None:
        self.sent.append(message)

    def recv(self):
        async def _gen():
            for event in self._events:
                yield event
            if self._error is not None:
                raise self._error

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


def _make_manager(
    tmp_path: Path,
) -> tuple[AgentManager, SqliteRepository, WebSocketClientPool]:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'witty.sqlite3'}")
    init_db(engine, auto_create=True)
    repository = SqliteRepository(create_session_factory(engine))
    repository.create_agent_with_id(
        agent_id=AGENT_ID,
        name="recovery",
        sandbox_type="local_process",
        adapter_type="opencode",
        workspace_path=str(tmp_path / "workspace"),
        idle_timeout_seconds=300,
        status=AgentStatus.running,
    )
    repository.save_sandbox_state(
        AGENT_ID,
        sandbox_payload_json={
            "sandbox_id": "sandbox-recovery",
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
        status="running",
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


async def _consume(
    manager: AgentManager, pool: WebSocketClientPool, client: _ScriptedWsClient
) -> list[dict[str, Any]]:
    with patch.object(pool, "get_client", return_value=client):
        received: list[dict[str, Any]] = []
        async for chunk in manager.send_message_stream(AGENT_ID, SESSION_ID, "分析 redis"):
            received.append(chunk["event"])
        return received


def _assistant_message(repository: SqliteRepository) -> MessageORM:
    with repository._session_factory() as db:  # noqa: SLF001 - 测试直接查库断言
        row = (
            db.query(MessageORM)
            .filter(MessageORM.session_id == SESSION_ID, MessageORM.role == "assistant")
            .one()
        )
        db.expunge(row)
        return row


def _event_row_count(repository: SqliteRepository) -> int:
    with repository._session_factory() as db:  # noqa: SLF001
        return (
            db.query(MessageEventORM)
            .filter(MessageEventORM.session_id == SESSION_ID)
            .count()
        )


def _session_status(repository: SqliteRepository) -> str:
    with repository._session_factory() as db:  # noqa: SLF001
        row = db.get(SessionORM, SESSION_ID)
        assert row is not None
        return row.status.value


def test_stream_disconnect_finalizes_message_and_session(tmp_path: Path) -> None:
    """ws 中途断开：已收到的事件必须落库、正文保留、消息与会话都退出生成态。"""

    async def run() -> None:
        manager, repository, pool = _make_manager(tmp_path)
        events = [
            _event("session.state_changed", {"state": "running", "seq": 1}),
            _event("thinking.delta", {"delta": "思考"}),
            *[_event("message.delta", {"delta": f"片段{i}"}) for i in range(200)],
        ]
        client = _ScriptedWsClient(
            events,
            error=AdaptorReceiveError(
                message="WebSocket connection closed unexpectedly",
                details={"code": 1011, "reason": "keepalive ping timeout"},
            ),
        )

        received = await _consume(manager, pool, client)

        assert received[-1]["type"] == "stream.error"
        assert received[-1]["payload"]["code"] == "CONSUMER_ERROR"
        assert sum(1 for e in received if e["type"] == "message.delta") == 200

        message = _assistant_message(repository)
        assert message.status == MessageStatus.interrupted
        # 批量落库最后一批（不足 64 条）也必须被 finally 里的 flush 写进去
        assert message.content == "".join(f"片段{i}" for i in range(200))
        # thinking.delta ×1 + message.delta ×200
        assert _event_row_count(repository) == 201
        assert _session_status(repository) == "idle"

    asyncio.run(run())


def test_stream_completion_persists_all_batched_events(tmp_path: Path) -> None:
    """正常完成路径：批量落库不丢事件，消息落定 completed。"""

    async def run() -> None:
        manager, repository, pool = _make_manager(tmp_path)
        text = "".join(f"t{i}" for i in range(150))
        events = [
            *[_event("message.delta", {"delta": f"t{i}"}) for i in range(150)],
            _event("message.completed", {"text": text}),
        ]
        client = _ScriptedWsClient(events)

        received = await _consume(manager, pool, client)

        assert [e["type"] for e in received][-1] == "message.completed"
        message = _assistant_message(repository)
        assert message.status == MessageStatus.completed
        assert message.content == text
        # message.completed 会把 150 条 message.delta 压缩成一条分段事件
        assert _event_row_count(repository) == 2

    asyncio.run(run())
