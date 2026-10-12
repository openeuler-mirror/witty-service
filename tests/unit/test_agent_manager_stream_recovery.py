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
        async for chunk in manager.send_message_stream(
            AGENT_ID, SESSION_ID, "分析 redis"
        ):
            received.append(chunk["event"])
        return received


def _assistant_message(repository: SqliteRepository) -> MessageORM:
    with repository._session_factory() as db:
        row = (
            db.query(MessageORM)
            .filter(MessageORM.session_id == SESSION_ID, MessageORM.role == "assistant")
            .one()
        )
        db.expunge(row)
        return row


def _event_row_count(repository: SqliteRepository) -> int:
    with repository._session_factory() as db:
        return (
            db.query(MessageEventORM)
            .filter(MessageEventORM.session_id == SESSION_ID)
            .count()
        )


def _session_status(repository: SqliteRepository) -> str:
    with repository._session_factory() as db:
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
        # 统一错误契约：后端消费者异常归类 INTERNAL_ERROR（agent-server
        # stream_errors.py 词表），不再用 CONSUMER_ERROR。
        assert received[-1]["payload"]["code"] == "INTERNAL_ERROR"
        assert received[-1]["payload"]["source"] == "internal"
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


def _single_event_payload(
    repository: SqliteRepository, event_type: str
) -> dict[str, Any]:
    """取该 session 下唯一一条指定类型事件的 payload。"""
    with repository._session_factory() as db:
        row = (
            db.query(MessageEventORM)
            .filter(
                MessageEventORM.session_id == SESSION_ID,
                MessageEventORM.event_type == event_type,
            )
            .one()
        )
        return dict(row.payload_json or {})


def test_stream_error_event_is_persisted_with_message(tmp_path: Path) -> None:
    """stream.error 终态事件随消息时间线落库：刷新后前端能还原错误码与根因。"""

    async def run() -> None:
        manager, repository, pool = _make_manager(tmp_path)
        events = [
            _event("message.delta", {"delta": "前半"}),
            _event(
                "stream.error",
                {
                    "code": "UPSTREAM_QUOTA_EXCEEDED",
                    "message": "Insufficient Balance",
                    "source": "upstream",
                    "retryable": False,
                    "detail": {"request_id": "req-1"},
                },
            ),
        ]
        client = _ScriptedWsClient(events)

        received = await _consume(manager, pool, client)

        assert received[-1]["type"] == "stream.error"
        message = _assistant_message(repository)
        assert message.status == MessageStatus.error
        assert message.content == "前半"
        # message.delta ×1 + stream.error ×1
        assert _event_row_count(repository) == 2
        assert _single_event_payload(repository, "stream.error") == {
            "code": "UPSTREAM_QUOTA_EXCEEDED",
            "message": "Insufficient Balance",
            "source": "upstream",
            "retryable": False,
            "detail": {"request_id": "req-1"},
        }
        # 会话状态由 _sync_session_state_from_event 在收到 stream.error 时置为
        # error（既有的语义：标记上轮失败），下一轮提问时复位 running。
        assert _session_status(repository) == "error"

    asyncio.run(run())


def test_stream_error_before_any_content_creates_errored_message(
    tmp_path: Path,
) -> None:
    """错误先于任何内容事件到达：兜底建空 assistant 消息，错误事件照样落库。"""

    async def run() -> None:
        manager, repository, pool = _make_manager(tmp_path)
        events = [
            _event(
                "stream.error",
                {
                    "code": "TRANSPORT_CLOSED",
                    "message": "connection closed",
                    "source": "transport",
                    "retryable": True,
                },
            ),
        ]
        client = _ScriptedWsClient(events)

        received = await _consume(manager, pool, client)

        assert received[-1]["type"] == "stream.error"
        message = _assistant_message(repository)
        assert message.status == MessageStatus.error
        assert message.content == ""
        assert _event_row_count(repository) == 1
        assert _single_event_payload(repository, "stream.error")["code"] == (
            "TRANSPORT_CLOSED"
        )
        # 同上：会话被 _sync_session_state_from_event 置为 error，下轮提问复位。
        assert _session_status(repository) == "error"

    asyncio.run(run())
