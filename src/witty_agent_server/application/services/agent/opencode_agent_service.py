from __future__ import annotations

import logging
from typing import Any

from witty_agent_server.application.models.agent import Agent
from witty_agent_server.application.models.agent import AgentStatus
from witty_agent_server.application.services.agent.base import (
    AgentServiceBase,
    OpenCodeLifecycleControlPort,
)
from witty_agent_server.application.services.agent.errors import AgentServiceError
from witty_agent_server.application.services.agent.opencode_lifecycle_service import (
    OpenCodeLifecycleError,
    OpenCodeLifecycleService,
)
from witty_agent_server.infra.clients.opencode_client import OpenCodeClient
from witty_agent_server.runtimes.runtime_base import RuntimeType


logger = logging.getLogger(__name__)


class OpenCodeAgentService(AgentServiceBase):
    """opencode runtime 的 agent 服务。

    通过 ``OpenCodeLifecycleService`` 控制 ``opencode serve`` 进程生命周期，
    通过 ``OpenCodeClient`` 与 serve HTTP API 通信。
    """

    def __init__(
        self,
        agent: Agent | None = None,
        lifecycle_service: OpenCodeLifecycleControlPort | None = None,
        client: OpenCodeClient | None = None,
        runtime: RuntimeType = "opencode",
    ) -> None:
        super().__init__(agent=agent, runtime=runtime)
        self._client: OpenCodeClient = client or OpenCodeClient()
        self._lifecycle_service: OpenCodeLifecycleControlPort = (
            lifecycle_service or OpenCodeLifecycleService(client=self._client)
        )

    def start(
        self,
        *,
        agent_id: str | None = None,
        config: dict[str, Any] | None = None,
        reload: bool = True,
    ) -> Agent:
        """启动 opencode agent"""
        with self._lock:
            self._last_start_already_running = False
            if config is not None:
                self._agent.config = dict(config)
            if agent_id is not None:
                self._agent.id = agent_id
            elif self._agent.id is None:
                self._agent.id = "main"

            logger.info(
                "agent start requested: agent_id=%s runtime=%s reload=%s",
                self._agent.id,
                self._runtime,
                reload,
            )

            self._apply_config(config)

        # ---- 锁外执行：进程生命周期操作不持有 agent 锁 ----
        is_running = self._lifecycle_service.probe_running()
        if is_running and not reload:
            with self._lock:
                self._last_start_already_running = True
                self._agent.status = AgentStatus.RUNNING
            logger.info(
                "agent start reused running opencode serve: agent_id=%s",
                self._agent.id,
            )
            return self.agent

        if is_running:
            self._lifecycle_service.stop()

        try:
            self._lifecycle_service.start_server()
        except OpenCodeLifecycleError as exc:
            # 进程此刻必定不存活（旧进程已 stop / 新进程未起），
            # 把状态置 FAILED 让调用方感知失败；
            with self._lock:
                self._agent.status = AgentStatus.FAILED
            raise AgentServiceError(
                code="OPENCODE_SERVE_START_FAILED",
                message="opencode serve start failed",
                status_code=500,
                details={"action": exc.action, "message": exc.message},
            ) from exc

        with self._lock:
            self._agent.status = AgentStatus.RUNNING
        logger.info(
            "agent start completed: agent_id=%s runtime=%s",
            self._agent.id,
            self._runtime,
        )
        return self.agent

    def _apply_config(self, config: dict[str, Any] | None) -> None:
        if not config:
            return
        oc_cfg = config.get("opencode")
        if not isinstance(oc_cfg, dict):
            return

        client_kwargs: dict[str, Any] = {}
        for key in ("serve_port", "username", "password", "timeout"):
            val = oc_cfg.get(key)
            if val is not None:
                client_kwargs[key] = val
        if client_kwargs:
            self._client.update_config(**client_kwargs)

        config_dir = oc_cfg.get("config_dir")
        if config_dir is not None:
            self._lifecycle_service.update_config(config_dir=config_dir)

    def stop(self, *, agent_id: str | None = None) -> Agent:
        """停止 opencode serve 进程并将状态切换为 stopped。

        若进程在 dispose + terminate + kill 后仍存活，将状态置为 FAILED
        """
        with self._lock:
            self._ensure_agent_context(agent_id=agent_id)

        # ---- 锁外执行：进程生命周期操作不持有 agent 锁 ----
        try:
            self._lifecycle_service.stop()
        except OpenCodeLifecycleError:
            logger.warning(
                "opencode serve process survived stop, marking agent FAILED",
                exc_info=True,
            )
            with self._lock:
                self._agent.status = AgentStatus.FAILED
            return self.agent

        with self._lock:
            self._transition(
                allowed_current=(AgentStatus.RUNNING, AgentStatus.PAUSED),
                target=AgentStatus.STOPPED,
            )

        logger.info(
            "agent stop completed: agent_id=%s runtime=%s",
            self._agent.id,
            self._runtime,
        )
        return self.agent

    def list_agents(self) -> dict[str, Any]:
        return self._client.list_agents()

    def resolve_default_agent(self) -> str:
        return self._agent.id or "main"
