"""并发任务池：按 session 控制串行，跨 session 并发执行。"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from witty_agent_server.application.services.session_ws_orchestrator import (
        SessionWSOrchestrator,
    )


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]


class SessionBusyError(RuntimeError):
    """同一会话已有在途任务时抛出。"""

    code = "SESSION_BUSY"
    message = "session is busy"


class TaskPool:
    """
    管理会话任务并发执行：
    - 不同 session 并发
    - 同 session 串行
    """

    def __init__(self, orchestrator: SessionWSOrchestrator) -> None:
        self._orchestrator = orchestrator
        self._inflight_sessions: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()
        self._cancel_events: dict[tuple[str, str], threading.Event] = {}
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def submit(
        self,
        *,
        agent_id: str,
        session_id: str,
        message: str,
        on_event: EventCallback,
    ) -> None:
        """
        提交任务到后台执行。

        Raises:
            SessionBusyError: 同一 session 已有在途任务
            SessionWSOrchestratorError: 前置校验失败
        """
        self._orchestrator.precheck_message(
            agent_id=agent_id,
            session_id=session_id,
            message=message,
        )

        session_scope = (agent_id, session_id)
        async with self._lock:
            if session_scope in self._inflight_sessions:
                raise SessionBusyError()
            self._inflight_sessions.add(session_scope)

        task = asyncio.create_task(
            self._run_turn(
                agent_id=agent_id,
                session_id=session_id,
                message=message,
                on_event=on_event,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def abort_session(self, agent_id: str, session_id: str) -> bool:
        """
        中止 runtime turn,通知生产者线程停止运行。

        Returns:
            该 session 当前是否有正在执行的 turn,有为 True,无则为 False。
        """
        key = (agent_id, session_id)
        event = self._cancel_events.get(key)
        if event is not None:
            event.set()
            self._orchestrator.abort_turn(agent_id=agent_id, session_id=session_id)
        return event is not None

    def answer_question(
        self,
        *,
        agent_id: str,
        session_id: str,
        request_id: str,
        answers: list[list[str]],
    ) -> bool:
        """回答提问事件，使 SSE 流继续。"""
        return self._orchestrator.answer_question(
            agent_id=agent_id,
            session_id=session_id,
            request_id=request_id,
            answers=answers,
        )

    def reject_question(
        self, *, agent_id: str, session_id: str, request_id: str
    ) -> bool:
        """拒绝提问事件，使 SSE 流继续。"""
        return self._orchestrator.reject_question(
            agent_id=agent_id,
            session_id=session_id,
            request_id=request_id,
        )

    async def _run_turn(
        self,
        *,
        agent_id: str,
        session_id: str,
        message: str,
        on_event: EventCallback,
    ) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        cancel_event = threading.Event()
        session_scope = (agent_id, session_id)
        self._cancel_events[session_scope] = cancel_event

        def _producer() -> None:
            try:
                for item in self._orchestrator.stream_message(
                    agent_id=agent_id,
                    session_id=session_id,
                    message=message,
                ):
                    if cancel_event.is_set():
                        break
                    loop.call_soon_threadsafe(queue.put_nowait, dict(item))
            except Exception:
                logger.exception(
                    "stream producer error: agent_id=%s session_id=%s",
                    agent_id,
                    session_id,
                )
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {
                        "type": "stream.error",
                        "agent_id": agent_id,
                        "session_id": session_id,
                        "payload": {
                            "code": "PRODUCER_ERROR",
                            "message": "Internal stream producer encountered an error",
                        },
                    },
                )
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        producer_future = loop.run_in_executor(None, _producer)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                # 在转发 terminal 事件前释放 inflight 锁，让后续消息可以立即提交
                if event.get("type") in {"message.completed", "turn.completed"}:
                    async with self._lock:
                        self._inflight_sessions.discard(session_scope)
                await on_event(event)
        finally:
            async with self._lock:
                self._inflight_sessions.discard(
                    session_scope
                )  # no-op if already released above
            self._cancel_events.pop(session_scope, None)
            await producer_future
