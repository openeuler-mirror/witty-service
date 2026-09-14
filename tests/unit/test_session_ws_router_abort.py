"""消费端 WS 断开时的 in-flight 释放行为。

对应线上现象：witty-service 的消费连接被 keepalive 掐断后，agent-server 里的
那一轮还在跑，而同 session 的重新提问会一直收到 SESSION_BUSY —— 用户看到的是
"发消息没有任何反应"。这里断言：
  - 断开且本轮还没下发终态事件 → 主动 abort，释放 in-flight
  - 终态事件已经下发过（正常的"turn 结束后对端主动断开"）→ 不能误 abort
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI
from starlette.testclient import TestClient

from witty_agent_server.api.routers.session_ws_router import create_session_ws_router
from witty_agent_server.application.services.session_state_sync_service import (
    SessionStateSyncService,
)


class _FakeTaskPool:
    def __init__(self, *, emit_terminal: bool = False) -> None:
        self.aborted: list[tuple[str, str]] = []
        self.submitted: list[dict[str, Any]] = []
        self._emit_terminal = emit_terminal

    async def submit(
        self,
        *,
        agent_id: str,
        session_id: str,
        message: str,
        on_event: Any,
    ) -> None:
        self.submitted.append(
            {"agent_id": agent_id, "session_id": session_id, "message": message}
        )
        if self._emit_terminal:
            await on_event({"type": "turn.completed", "payload": {}})
        # 不真的跑 turn：真实场景里 runtime 仍在跑，in-flight 条目还在

    def abort_session(self, agent_id: str, session_id: str) -> bool:
        self.aborted.append((agent_id, session_id))
        return True

    def answer_question(self, **_: Any) -> bool:
        return True

    def reject_question(self, **_: Any) -> bool:
        return True


def _make_app(task_pool: _FakeTaskPool) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_session_ws_router(
            task_pool=task_pool,  # type: ignore[arg-type]
            state_sync_service=SessionStateSyncService(),
        )
    )
    return app


def _wait_until(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_disconnect_without_terminal_event_aborts_turn() -> None:
    pool = _FakeTaskPool()
    with TestClient(_make_app(pool)) as client:
        with client.websocket_connect("/agents/agent-1/sessions/session-1/ws") as ws:
            ws.send_json({"type": "message.create", "payload": {"message": "分析 redis"}})
            assert _wait_until(lambda: bool(pool.submitted))
        # 退出 with ⇒ 消费端断开；服务端应在 finally 里释放 in-flight
        assert _wait_until(lambda: bool(pool.aborted))

    assert pool.submitted == [
        {"agent_id": "agent-1", "session_id": "session-1", "message": "分析 redis"}
    ]
    assert pool.aborted == [("agent-1", "session-1")]


def test_disconnect_after_terminal_event_does_not_abort() -> None:
    pool = _FakeTaskPool(emit_terminal=True)
    with TestClient(_make_app(pool)) as client:
        with client.websocket_connect("/agents/agent-1/sessions/session-1/ws") as ws:
            ws.send_json({"type": "message.create", "payload": {"message": "hello"}})
            assert _wait_until(lambda: bool(pool.submitted))
            # 终态事件已经通过 on_event 交给对端（真实链路里 witty-service 收到
            # turn.completed 后主动断开），此时不该再 abort
            ws.receive_json()
        time.sleep(0.3)

    assert pool.aborted == []
