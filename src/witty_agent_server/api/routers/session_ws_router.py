from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from witty_agent_server.application.services.session_state_sync_service import (
    SessionStateSyncService,
)
from witty_agent_server.application.services.session_ws_orchestrator import (
    SessionWSOrchestratorError,
)
from witty_agent_server.application.services.task_pool import SessionBusyError, TaskPool

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


def create_session_ws_router(
    *,
    task_pool: TaskPool,
    registry: SessionConnectionRegistry | None = None,
    state_sync_service: SessionStateSyncService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/agents/{agent_id}/sessions")
    connection_registry = registry or SessionConnectionRegistry()
    resolved_state_sync = state_sync_service or SessionStateSyncService()

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
                            await outbound.put(
                                _client_error(
                                    agent_id=agent_id,
                                    session_id=session_id,
                                    code="INVALID_CLIENT_EVENT",
                                    message="invalid websocket payload",
                                )
                            )
                            continue

                        await _dispatch_event(
                            agent_id=agent_id,
                            session_id=session_id,
                            event=message,
                            task_pool=task_pool,
                            outbound=outbound,
                        )
                except WebSocketDisconnect:
                    return
                finally:
                    resolved_state_sync.unbind_connection(
                        agent_id=agent_id,
                        session_id=session_id,
                    )
                    await outbound.put(None)
                    await sender_task
        except SessionAlreadyConnectedError:
            await websocket.close(
                code=status.WS_1008_POLICY_VIOLATION,
                reason=SessionAlreadyConnectedError.code,
            )
        except WebSocketDisconnect:
            return

    return router


async def _dispatch_event(
    *,
    agent_id: str,
    session_id: str,
    event: dict[str, Any],
    task_pool: TaskPool,
    outbound: asyncio.Queue[dict[str, Any] | None],
) -> None:
    event_type = event.get("type")
    payload = event.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    if event_type == "message.abort":
        cancelled = task_pool.abort_session(agent_id=agent_id, session_id=session_id)
        return
    if event_type == "question.reply":
        await _dispatch_question_event(
            agent_id=agent_id,
            session_id=session_id,
            task_pool=task_pool,
            outbound=outbound,
            action_type="reply",
            request_id=payload.get("request_id"),
            answers=payload.get("answers"),
        )
        return
    if event_type == "question.reject":
        await _dispatch_question_event(
            agent_id=agent_id,
            session_id=session_id,
            task_pool=task_pool,
            outbound=outbound,
            action_type="reject",
            request_id=payload.get("request_id"),
        )
        return
    if event_type != "message.create":
        await outbound.put(
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
            on_event=outbound.put,
        )
    except SessionBusyError as exc:
        await outbound.put(
            _client_error(
                agent_id=agent_id,
                session_id=session_id,
                code=exc.code,
                message=exc.message,
            )
        )
    except SessionWSOrchestratorError as exc:
        await outbound.put(
            _client_error(
                agent_id=agent_id,
                session_id=session_id,
                code=exc.code,
                message=exc.message,
                details=exc.details,
            )
        )


async def _dispatch_question_event(
    *,
    agent_id: str,
    session_id: str,
    task_pool: TaskPool,
    outbound: asyncio.Queue[dict[str, Any] | None],
    action_type: str,
    request_id: Any,
    answers: Any = None,
) -> None:
    """处理 question.reply 与 question.reject 两类 WS 事件。"""
    logger.info(
        "received question.%s: agent_id=%s session_id=%s request_id=%s",
        action_type,
        agent_id,
        session_id,
        request_id,
    )
    if not isinstance(request_id, str) or not request_id:
        await outbound.put(
            _client_error(
                agent_id=agent_id,
                session_id=session_id,
                code=f"INVALID_QUESTION_{action_type.upper()}",
                message=f"question.{action_type} requires a valid request_id",
            )
        )
        return

    if action_type == "reply" and not isinstance(answers, list):
        answers = []

    try:
        if action_type == "reply":
            task_pool.answer_question(
                agent_id=agent_id,
                session_id=session_id,
                request_id=request_id,
                answers=answers,
            )
        else:
            task_pool.reject_question(
                agent_id=agent_id,
                session_id=session_id,
                request_id=request_id,
            )
        logger.info(
            "question.%s succeeded: agent_id=%s session_id=%s request_id=%s",
            action_type,
            agent_id,
            session_id,
            request_id,
        )
    except SessionWSOrchestratorError as exc:
        logger.warning(
            "question.%s orchestrator error: agent_id=%s session_id=%s request_id=%s code=%s message=%s",
            action_type,
            agent_id,
            session_id,
            request_id,
            exc.code,
            exc.message,
        )
        await outbound.put(
            _client_error(
                agent_id=agent_id,
                session_id=session_id,
                code=exc.code,
                message=exc.message,
                details=exc.details,
            )
        )
    except Exception:
        logger.exception(
            "question.%s unexpected error: agent_id=%s session_id=%s request_id=%s",
            action_type,
            agent_id,
            session_id,
            request_id,
        )
        await outbound.put(
            _client_error(
                agent_id=agent_id,
                session_id=session_id,
                code=f"QUESTION_{action_type.upper()}_FAILED",
                message=f"failed to process question {action_type}",
            )
        )


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
