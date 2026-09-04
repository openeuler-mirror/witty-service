from __future__ import annotations

import logging
from typing import Any

from witty_agent_server.application.models.agent import Agent, AgentStatus
from witty_agent_server.application.services.agent.base import (
    AgentServiceBase,
    DshLifecycleControlPort,
)
from witty_agent_server.application.services.agent.dsh_lifecycle_service import (
    DshLifecycleError,
    DshLifecycleService,
)
from witty_agent_server.application.services.agent.errors import AgentServiceError
from witty_agent_server.infra.clients.dsh_client import DshClient
from witty_agent_server.runtimes.runtime_base import RuntimeType

logger = logging.getLogger(__name__)


class DshAgentService(AgentServiceBase):
    """dsh runtime 的 agent 服务。

    通过 ``DshLifecycleService`` 控制 ``DeepSeekHarness`` 生命周期，
    通过 ``DshClient`` 与 dsh SDK 通信。
    """

    def __init__(
        self,
        agent: Agent | None = None,
        lifecycle_service: DshLifecycleControlPort | None = None,
        client: DshClient | None = None,
        runtime: RuntimeType = "dsh",
    ) -> None:
        super().__init__(agent=agent, runtime=runtime)
        self._client: DshClient = client or DshClient()
        self._lifecycle_service: DshLifecycleControlPort = (
            lifecycle_service or DshLifecycleService(client=self._client)
        )

    def start(
        self,
        *,
        agent_id: str | None = None,
        config: dict[str, Any] | None = None,
        reload: bool = True,
    ) -> Agent:
        """启动 dsh agent"""
        with self._lock:
            self._last_start_already_running = False
            # 只更新内存态配置（剥离敏感字段），进程侧下推在锁外完成。
            if config is not None:
                self._agent.config = self._sanitize_config(config)
            # 先解析 agent_id 但不落库：校验通过后才写入 self._agent.id。
            resolved_agent_id = (
                agent_id if agent_id is not None else (self._agent.id or "main")
            )

            logger.info(
                "agent start requested: agent_id=%s runtime=%s reload=%s",
                resolved_agent_id,
                self._runtime,
                reload,
            )

        # ---- 锁外执行：配置下推可能 detach/close harness（进程操作） ----
        self._apply_config(config, agent_id=resolved_agent_id)

        with self._lock:
            self._agent.id = resolved_agent_id

        # ---- 锁外执行：进程生命周期操作不持有 agent 锁 ----
        is_running = self._lifecycle_service.probe_running()
        if is_running and not reload:
            with self._lock:
                self._last_start_already_running = True
                self._agent.status = AgentStatus.RUNNING
            logger.info(
                "agent start reused running dsh harness: agent_id=%s",
                self._agent.id,
            )
            return self.agent

        if is_running:
            self._lifecycle_service.stop()

        try:
            self._lifecycle_service.start_server()
        except DshLifecycleError as exc:
            # harness 此刻必定不存活，置 FAILED 让调用方感知失败。
            with self._lock:
                self._agent.status = AgentStatus.FAILED
            raise AgentServiceError(
                code="DSH_RUNTIME_START_FAILED",
                message="dsh runtime start failed",
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

    def _apply_config(self, config: dict[str, Any] | None, *, agent_id: str) -> None:
        """从 ``config["dsh"]`` 提取模型配置下推 lifecycle。

        workspace 路径由 lifecycle 按 agent_id 推导，故 agent_id 始终下推
        （无 dsh 配置时也保证实例目录隔离）；agent_id 校验失败即抛错不落库。
        """
        dsh_cfg = config.get("dsh") if isinstance(config, dict) else None
        kwargs: dict[str, Any] = {"agent_id": agent_id}
        if isinstance(dsh_cfg, dict):
            for key in ("provider", "model", "api_key", "base_url", "max_tokens"):
                val = dsh_cfg.get(key)
                if val is not None:
                    kwargs[key] = val
        try:
            self._lifecycle_service.update_config(**kwargs)
        except DshLifecycleError as exc:
            raise AgentServiceError(
                code="DSH_AGENT_CONFIG_INVALID",
                message=f"invalid dsh agent config: {exc.message}",
                status_code=400,
                details={"action": exc.action, "message": exc.message},
            ) from exc

    @staticmethod
    def _sanitize_config(config: dict[str, Any]) -> dict[str, Any]:
        """拷贝 config 并剥离敏感字段（dsh.api_key / model.api_key），避免响应/落库回显凭据。"""
        sanitized = dict(config)
        for section in ("dsh", "model"):
            section_cfg = sanitized.get(section)
            if isinstance(section_cfg, dict):
                sanitized[section] = {
                    k: v for k, v in section_cfg.items() if k != "api_key"
                }
        return sanitized

    def stop(self, *, agent_id: str | None = None) -> Agent:
        """停止 dsh harness 并将状态切换为 stopped。

        若 harness 在 SDK 终止兜底后仍存活（stop 抛 DshLifecycleError），
        将状态置为 FAILED。
        """
        with self._lock:
            self._ensure_agent_context(agent_id=agent_id)

        # ---- 锁外执行：进程生命周期操作不持有 agent 锁 ----
        try:
            self._lifecycle_service.stop()
        except DshLifecycleError:
            logger.warning(
                "dsh harness survived stop, marking agent FAILED",
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

    # setup_mcp / unset_mcp 暂不实现（dsh MCP 走 cordis 配置，
    # 后续演进项），保持基类 NotImplementedError。
