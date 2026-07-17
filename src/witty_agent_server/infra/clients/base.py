from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any


class ClientBase(ABC):
    @abstractmethod
    def list_agents(self) -> dict[str, Any]:
        """返回 gateway 已加载 agent 列表及默认 agent 信息。"""

    @abstractmethod
    def list_sessions(self, *, agent_id: str) -> dict[str, Any]:
        """返回指定 agent 在 gateway 可见的会话列表。"""

    @abstractmethod
    def get_agent(self, *, agent_id: str) -> dict[str, Any] | None:
        """查询网关侧是否已经加载指定 agent。"""

    @abstractmethod
    def get_skills_status(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """查询指定 agent 可见的技能状态。"""

    @abstractmethod
    def create_session(self, *, session_key: str) -> None:
        """创建网关会话。"""

    @abstractmethod
    def delete_session(self, *, session_key: str) -> None:
        """删除网关会话。"""

    @abstractmethod
    def abort_session(self, *, session_key: str) -> None:
        """中止网关会话执行。"""

    @abstractmethod
    def stream_turn(
        self, *, session_key: str, message: str
    ) -> Iterator[dict[str, Any]]:
        """流式执行单轮并返回网关原始事件。"""
