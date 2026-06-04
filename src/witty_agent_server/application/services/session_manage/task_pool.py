"""并发任务池：按 session 控制串行，跨 session 并发执行。"""

from __future__ import annotations

import asyncio

from witty_agent_server.application.services.session_manage.base import (
    EventCallback,
    SessionTaskPoolBase,
    SessionTurnExecutorBase,
)


class SessionBusyError(RuntimeError):
    """同一会话已有在途任务时抛出。"""

    code = "SESSION_BUSY"
    message = "session is busy"


class TaskPool(SessionTaskPoolBase):
    """
    管理会话任务并发执行：
    - 不同 session 并发
    - 同 session 串行
    """

    def __init__(self, orchestrator: SessionTurnExecutorBase) -> None:
        self._orchestrator = orchestrator
        self._inflight_sessions: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()

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

        asyncio.create_task(
            self._run_turn(
                agent_id=agent_id,
                session_id=session_id,
                message=message,
                on_event=on_event,
            )
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

        def _producer() -> None:
            try:
                for item in self._orchestrator.stream_message(
                    agent_id=agent_id,
                    session_id=session_id,
                    message=message,
                ):
                    loop.call_soon_threadsafe(queue.put_nowait, dict(item))
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, None)

        producer_future = loop.run_in_executor(None, _producer)
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                await on_event(event)
        finally:
            async with self._lock:
                self._inflight_sessions.discard((agent_id, session_id))
            await producer_future
