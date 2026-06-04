from __future__ import annotations

import logging

from witty_agent_server.application.models.agent import Agent
from witty_agent_server.application.models.agent_start import AgentStartRequest
from witty_agent_server.application.services.agent.base import AgentServiceBase
from witty_agent_server.application.models.agent import AgentStatus
from witty_agent_server.runtimes.runtime_base import RuntimeType


logger = logging.getLogger(__name__)


class OpenCodeAgentService(AgentServiceBase):
    """opencode runtime 的最小本地状态实现。"""

    def __init__(
        self,
        agent: Agent | None = None,
        runtime: RuntimeType = "opencode",
    ) -> None:
        super().__init__(agent=agent, runtime=runtime)

    def start(
        self,
        *,
        config: AgentStartRequest | None = None,
        reload: bool = False,
    ) -> Agent:
        """启动 opencode agent，仅维护本地状态，不访问 Gateway。"""
        with self._lock:
            self._last_start_already_running = False
            requested_agent_id = config.agent_id if config is not None else None
            if config is not None:
                self._agent.config = config.model_dump(
                    exclude_none=True,
                    exclude={"agent_id"},
                )
            if requested_agent_id is not None:
                self._agent.id = requested_agent_id
            elif self._agent.id is None:
                self._agent.id = "main"

            logger.info(
                "agent start requested: agent_id=%s runtime=%s reload=%s",
                self._agent.id,
                self._runtime,
                reload,
            )
            self._agent.status = AgentStatus.RUNNING
            logger.info(
                "agent start completed: agent_id=%s runtime=%s",
                self._agent.id,
                self._runtime,
            )
            return self.agent

    def list_agents(self) -> dict[str, Any]:
        agent_id = self._agent.id or "main"
        return {
            "defaultId": agent_id,
            "agents": [{"id": agent_id, "default": True, "loaded": True}],
        }

    def resolve_default_agent(self) -> str:
        return self._agent.id or "main"
