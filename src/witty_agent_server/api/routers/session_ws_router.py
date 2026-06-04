from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from witty_agent_server.application.services.session_manage.state_sync_service import (
    SessionStateSyncService,
)
from witty_agent_server.application.services.session_manage.orchestrator import (
    SessionWSOrchestratorError,
)
from witty_agent_server.application.services.session_manage.base import (
    EventCallback,
    SessionStateEventPublisherBase,
    SessionTaskPoolBase,
)
from witty_agent_server.application.services.session_manage.task_pool import SessionBusyError


logger = logging.getLogger(__name__)


class SessionConnectionRegistry:
    """确保同一 session 同时只有一个活动 WS 连接。"""

    def __init__(self) -> None:
        self._sessions: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def hold(self, *, agent_id: str, session_id: str) -> AsyncIterator[None]:
        session_scope = (agent_id, session_id)
        async with self._lock:
            if session_scope in self._sessions:
                raise SessionAlreadyConnectedError()
            self._sessions.add(session_scope)
        try:
            yield
        finally:
            async with self._lock:
                self._sessions.discard(session_scope)


class SessionAlreadyConnectedError(RuntimeError):
    code = "SESSION_ALREADY_CONNECTED"
    message = "session already connected"


class SessionMessageDispatcher:
    """负责处理客户端事件到任务池的映射。"""

    async def dispatch(
        self,
        *,
        agent_id: str,
        session_id: str,
        event: dict[str, Any],
        task_pool: SessionTaskPoolBase,
        on_event: EventCallback,
    ) -> None:
        event_type = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        if event_type != "message.create":
            await on_event(
                _client_error(
                    agent_id=agent_id,
                    session_id=session_id,
                    code="UNSUPPORTED_CLIENT_EVENT",
                    message="unsupported client event",
                )
            )
            return

        message = payload.get("message")
        text_message = message if isinstance(message, str) else ""
        try:
            await task_pool.submit(
                agent_id=agent_id,
                session_id=session_id,
                message=text_message,
                on_event=on_event,
            )
        except SessionBusyError as exc:
            await on_event(
                _client_error(
                    agent_id=agent_id,
                    session_id=session_id,
                    code=exc.code,
                    message=exc.message,
                )
            )
        except SessionWSOrchestratorError as exc:
            await on_event(
                _client_error(
                    agent_id=agent_id,
                    session_id=session_id,
                    code=exc.code,
                    message=exc.message,
                    details=exc.details,
                )
            )


def create_session_ws_router(
    *,
    task_pool: SessionTaskPoolBase,
    registry: SessionConnectionRegistry | None = None,
    state_sync_service: SessionStateEventPublisherBase | None = None,
    dispatcher: SessionMessageDispatcher | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/agents/{agent_id}/sessions")
    connection_registry = registry or SessionConnectionRegistry()
    resolved_state_sync: SessionStateEventPublisherBase = (
        state_sync_service or SessionStateSyncService()
    )
    resolved_dispatcher = dispatcher or SessionMessageDispatcher()

    @router.websocket("/{session_id}/ws")
    async def session_ws(websocket: WebSocket, agent_id: str, session_id: str) -> None:
        await websocket.accept()

        try:
            async with connection_registry.hold(agent_id=agent_id, session_id=session_id):
                outbound: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
                loop = asyncio.get_running_loop()
                resolved_state_sync.bind_connection(
                    agent_id=agent_id,
                    session_id=session_id,
                    loop=loop,
                    queue=outbound,
                )
                sender_task = asyncio.create_task(_sender(websocket, outbound))
                try:
                    while True:
                        message = await websocket.receive_json()
                        if not isinstance(message, dict):
                            logger.warning(
                                "invalid websocket payload: agent_id=%s session_id=%s",
                                agent_id,
                                session_id,
                            )
                            await outbound.put(
                                _client_error(
                                    agent_id=agent_id,
                                    session_id=session_id,
                                    code="INVALID_CLIENT_EVENT",
                                    message="invalid websocket payload",
                                )
                            )
                            continue

                        await resolved_dispatcher.dispatch(
                            agent_id=agent_id,
                            session_id=session_id,
                            event=message,
                            task_pool=task_pool,
                            on_event=outbound.put,
                        )
                except WebSocketDisconnect:
                    logger.info(
                        "websocket disconnected: agent_id=%s session_id=%s",
                        agent_id,
                        session_id,
                    )
                    return
                finally:
                    resolved_state_sync.unbind_connection(
                        agent_id=agent_id,
                        session_id=session_id,
                    )
                    await outbound.put(None)
                    await sender_task
        except SessionAlreadyConnectedError:
            logger.warning(
                "session already connected: agent_id=%s session_id=%s",
                agent_id,
                session_id,
            )
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason=SessionAlreadyConnectedError.code,
            )
        except WebSocketDisconnect:
            return

    return router


async def _sender(
    websocket: WebSocket,
    outbound: asyncio.Queue[dict[str, Any] | None],
) -> None:
    while True:
        item = await outbound.get()
        if item is None:
            return
        await websocket.send_json(item)


def _client_error(
    *,
    agent_id: str,
    session_id: str,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": code,
        "message": message,
    }
    if details is not None:
        payload["details"] = details
    return {
        "type": "client.error",
        "agent_id": agent_id,
        "session_id": session_id,
        "payload": payload,
    }
