from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from threading import RLock
from typing import Any

from witty_agent_server.application.models.runtime_events import build_outbound_event
from witty_agent_server.application.services.session_manage.base import (
    SessionState,
    SessionStateEventPublisherBase,
)
from witty_agent_server.runtimes.runtime_base import RuntimeType


logger = logging.getLogger(__name__)

@dataclass
class _SessionStateSnapshot:
    agent_id: str
    runtime_type: RuntimeType
    state: SessionState
    seq: int


@dataclass
class _SessionQueueBinding:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any] | None]


class SessionStateSyncService(SessionStateEventPublisherBase):
    """管理会话状态事件与 WS 推送通道。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._states: dict[tuple[str, str], _SessionStateSnapshot] = {}
        self._bindings: dict[tuple[str, str], _SessionQueueBinding] = {}

    def bind_connection(
        self,
        *,
        agent_id: str,
        session_id: str,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        """绑定 session 到当前活动连接的发送队列。"""
        with self._lock:
            self._bindings[(agent_id, session_id)] = _SessionQueueBinding(
                loop=loop,
                queue=queue,
            )
        logger.info("session ws bound: agent_id=%s session_id=%s", agent_id, session_id)

    def unbind_connection(self, *, agent_id: str, session_id: str) -> None:
        """解绑 session 的活动连接。"""
        with self._lock:
            self._bindings.pop((agent_id, session_id), None)
        logger.info(
            "session ws unbound: agent_id=%s session_id=%s",
            agent_id,
            session_id,
        )

    def build_state_changed_event(
        self,
        *,
        agent_id: str,
        session_id: str,
        runtime_type: RuntimeType,
        state: SessionState,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        """构建状态变化事件；若状态未变化则返回 None。"""
        scope_key = (agent_id, session_id)
        with self._lock:
            snapshot = self._states.get(scope_key)
            if snapshot is not None and snapshot.state == state:
                return None
            seq = 1 if snapshot is None else snapshot.seq + 1
            self._states[scope_key] = _SessionStateSnapshot(
                agent_id=agent_id,
                runtime_type=runtime_type,
                state=state,
                seq=seq,
            )

        payload: dict[str, Any] = {"state": state, "seq": seq}
        if isinstance(reason, str) and reason:
            payload["reason"] = reason

        logger.info(
            "session state changed: agent_id=%s session_id=%s state=%s seq=%s reason=%s",
            agent_id,
            session_id,
            state,
            seq,
            reason,
        )
        return build_outbound_event(
            agent_id=agent_id,
            session_id=session_id,
            runtime_type=runtime_type,
            type="session.state_changed",
            payload=payload,
        )

    def emit_event(self, *, agent_id: str, session_id: str, event: dict[str, Any]) -> bool:
        """向当前连接实时发送事件。"""
        with self._lock:
            binding = self._bindings.get((agent_id, session_id))
        if binding is None:
            return False

        binding.loop.call_soon_threadsafe(binding.queue.put_nowait, event)
        return True

    def emit_state_changed(
        self,
        *,
        agent_id: str,
        session_id: str,
        runtime_type: RuntimeType,
        state: SessionState,
        reason: str | None = None,
    ) -> bool:
        """构建并发送状态变化事件。"""
        event = self.build_state_changed_event(
            agent_id=agent_id,
            session_id=session_id,
            runtime_type=runtime_type,
            state=state,
            reason=reason,
        )
        if event is None:
            return False
        return self.emit_event(agent_id=agent_id, session_id=session_id, event=event)

    def emit_heartbeat(
        self,
        *,
        agent_id: str,
        session_id: str,
        runtime_type: RuntimeType,
    ) -> bool:
        """发送 session 心跳事件，便于上游感知连接活性。"""
        event = build_outbound_event(
            agent_id=agent_id,
            session_id=session_id,
            runtime_type=runtime_type,
            type="session.heartbeat",
            payload={},
        )
        logger.debug(
            "emit session heartbeat: agent_id=%s session_id=%s runtime_type=%s",
            agent_id,
            session_id,
            runtime_type,
        )
        return self.emit_event(agent_id=agent_id, session_id=session_id, event=event)
