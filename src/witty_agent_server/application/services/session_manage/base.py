from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator, Mapping
from typing import Any, Literal

from witty_agent_server.runtimes.runtime_base import RuntimeType


EventCallback = Callable[[dict[str, Any]], Awaitable[None]]
SessionState = Literal["running", "idle", "error"]


class SessionStateEventPublisherBase(ABC):
    """会话状态事件发布抽象基类。"""

    @abstractmethod
    def build_state_changed_event(
        self,
        *,
        agent_id: str,
        session_id: str,
        runtime_type: RuntimeType,
        state: SessionState,
        reason: str | None = None,
    ) -> dict[str, Any] | None:
        """构造会话状态变化事件。"""

    @abstractmethod
    def bind_connection(
        self,
        *,
        agent_id: str,
        session_id: str,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        """绑定会话到当前活动连接。"""

    @abstractmethod
    def unbind_connection(self, *, agent_id: str, session_id: str) -> None:
        """解绑会话对应的活动连接。"""

    @abstractmethod
    def emit_heartbeat(
        self,
        *,
        agent_id: str,
        session_id: str,
        runtime_type: RuntimeType,
    ) -> bool:
        """发送会话心跳事件。"""

    @abstractmethod
    def emit_state_changed(
        self,
        *,
        agent_id: str,
        session_id: str,
        runtime_type: RuntimeType,
        state: SessionState,
        reason: str | None = None,
    ) -> bool:
        """发送会话状态变化事件。"""


class SessionTurnExecutorBase(ABC):
    """会话轮次执行抽象基类。"""

    @abstractmethod
    def precheck_message(self, *, agent_id: str, session_id: str, message: str) -> None:
        """执行消息流转前的前置校验。"""

    @abstractmethod
    def stream_message(
        self,
        *,
        agent_id: str,
        session_id: str,
        message: str,
    ) -> Iterator[Mapping[str, Any]]:
        """流式执行单轮消息并产出事件。"""


class SessionTaskPoolBase(ABC):
    """WS 路由提交任务抽象基类。"""

    @abstractmethod
    async def submit(
        self,
        *,
        agent_id: str,
        session_id: str,
        message: str,
        on_event: EventCallback,
    ) -> None:
        """提交会话任务到后台执行。"""
