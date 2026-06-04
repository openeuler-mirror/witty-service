from __future__ import annotations

from abc import ABC, abstractmethod
from threading import RLock
from typing import Any

from witty_agent_server.application.models.agent import Agent, AgentStatus
from witty_agent_server.application.models.agent_start import AgentStartRequest
from witty_agent_server.runtimes.runtime_base import RuntimeType


class AgentServiceBase(ABC):
    """Agent 服务的共享状态与通用状态机。"""

    def __init__(
        self,
        *,
        agent: Agent | None = None,
        runtime: RuntimeType = "openclaw",
    ) -> None:
        self._agent = agent.model_copy(deep=True) if agent is not None else Agent()
        self._runtime: RuntimeType = runtime
        self._agent.runtime_type = self._runtime
        self._last_start_already_running = False
        self._lock = RLock()

    @property
    def agent(self) -> Agent:
        """返回当前 agent 的深拷贝，避免调用方直接修改内部状态。"""
        with self._lock:
            return self._agent.model_copy(deep=True)

    @property
    def last_start_already_running(self) -> bool:
        """返回最近一次 start 是否复用了已运行的 runtime。"""
        with self._lock:
            return self._last_start_already_running

    @abstractmethod
    def start(
        self,
        *,
        config: AgentStartRequest | None = None,
        reload: bool = False,
    ) -> Agent:
        """启动 agent。"""

    def stop(self, *, agent_id: str | None = None) -> Agent:
        """将 agent 状态切换为 stopped。"""
        with self._lock:
            self._ensure_agent_context(agent_id=agent_id)
            self._transition(
                allowed_current=(AgentStatus.RUNNING, AgentStatus.PAUSED),
                target=AgentStatus.STOPPED,
            )
            return self.agent

    def status(self, *, agent_id: str | None = None) -> Agent:
        """返回当前 agent 状态，并校验请求上下文。"""
        with self._lock:
            self._ensure_agent_context(agent_id=agent_id)
            return self.agent

    def list_agents(self) -> dict[str, Any]:
        """返回可见 agent 列表，默认不支持。"""
        raise NotImplementedError

    def resolve_default_agent(self) -> str:
        """解析默认 agent，默认不支持。"""
        raise NotImplementedError

    def pause(self) -> Agent:
        """将 agent 状态切换为 paused。"""
        with self._lock:
            self._transition(
                allowed_current=(AgentStatus.RUNNING,),
                target=AgentStatus.PAUSED,
            )
            return self.agent

    def resume(self) -> Agent:
        """将 agent 状态从 paused 恢复为 running。"""
        with self._lock:
            self._transition(
                allowed_current=(AgentStatus.PAUSED,),
                target=AgentStatus.RUNNING,
            )
            return self.agent

    def update_config(self, updates: dict[str, Any]) -> Agent:
        """在非运行态更新 agent config。"""
        with self._lock:
            if self._agent.status == AgentStatus.RUNNING:
                from witty_agent_server.application.services.agent.errors import (
                    AgentConfigUpdateForbiddenError,
                )

                raise AgentConfigUpdateForbiddenError()
            self._agent.config.update(updates)
            return self.agent

    def _transition(
        self,
        *,
        allowed_current: tuple[AgentStatus, ...],
        target: AgentStatus,
    ) -> None:
        """执行状态迁移校验并写入目标状态。"""
        self._ensure_transition_allowed(
            allowed_current=allowed_current,
            target=target,
        )
        self._agent.status = target

    def _ensure_transition_allowed(
        self,
        *,
        allowed_current: tuple[AgentStatus, ...],
        target: AgentStatus,
    ) -> None:
        """确保当前状态允许迁移到目标状态。"""
        current_status = self._agent.status
        if current_status not in allowed_current:
            from witty_agent_server.application.services.agent.errors import (
                InvalidAgentTransitionError,
            )

            raise InvalidAgentTransitionError(
                current=current_status,
                target=target,
            )

    def _ensure_agent_context(self, *, agent_id: str | None) -> None:
        """刷新当前快照中的 agent_id，但不再把它当作请求门禁。"""
        if not isinstance(agent_id, str) or not agent_id:
            return
        self._agent.id = agent_id
