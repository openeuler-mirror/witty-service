from __future__ import annotations

import logging
from collections.abc import Mapping
from threading import RLock
from typing import Any

from witty_agent_server.application.composition.runtime_instance_manager import (
    RuntimeInstanceManager,
)
from witty_agent_server.application.models.agent import Agent
from witty_agent_server.application.models.agent_start import AgentStartRequest
from witty_agent_server.application.services.agent.base import AgentServiceBase
from witty_agent_server.application.services.agent.errors import AgentServiceError
from witty_agent_server.runtimes.runtime_base import RuntimeType


logger = logging.getLogger(__name__)


class AgentFacadeService:
    """按 runtime_type 将 agent 请求分发到具体 runtime agent service。"""

    def __init__(
        self,
        *,
        services: Mapping[RuntimeType, AgentServiceBase],
        runtime_instance_manager: RuntimeInstanceManager | None = None,
        default_runtime_type: RuntimeType | None = None,
    ) -> None:
        self._services = dict(services)
        self._agent_runtime_index: dict[str, RuntimeType] = {}
        self._index_lock = RLock()
        self._runtime_instance_manager = runtime_instance_manager
        self._default_runtime_type: RuntimeType | None = default_runtime_type or next(
            iter(self._services),
            None,
        )
        if self._default_runtime_type is None:
            logger.error("agent facade requires at least one runtime service")
            raise ValueError("agent facade requires at least one runtime service")

    @property
    def last_start_already_running(self) -> bool:
        service = self._get_default_service()
        return bool(getattr(service, "last_start_already_running", False))

    @property
    def agent(self) -> Agent:
        """向下兼容单 runtime 服务接口，返回当前已索引 agent 的快照。"""
        service = self._get_default_service()
        return service.agent

    def start(
        self,
        *,
        config: AgentStartRequest | None = None,
        reload: bool = False,
    ) -> Agent:
        runtime_type = config.runtime_type if config is not None else None
        if not isinstance(runtime_type, str) or not runtime_type:
            logger.warning("agent start rejected: runtime_type is required")
            raise AgentServiceError(
                code="RUNTIME_TYPE_REQUIRED",
                message="runtime_type is required",
                status_code=400,
                details=None,
            )
        service = self._require_service(runtime_type)
        agent = service.start(config=config, reload=reload)
        if isinstance(agent.id, str) and agent.id:
            with self._index_lock:
                self._agent_runtime_index[agent.id] = runtime_type
            logger.info(
                "agent runtime indexed: agent_id=%s runtime_type=%s",
                agent.id,
                runtime_type,
            )
        return agent

    def stop(self, *, agent_id: str | None = None) -> Agent:
        service = self._resolve_service_by_agent_id(agent_id=agent_id)
        return service.stop(agent_id=agent_id)

    def status(self, *, agent_id: str | None = None) -> Agent:
        service = self._resolve_service_by_agent_id(agent_id=agent_id)
        return service.status(agent_id=agent_id)

    def resolve_default_agent(self) -> str:
        return self._get_default_service().resolve_default_agent()

    def list_agents(self) -> dict[str, Any]:
        return self._get_default_service().list_agents()

    def _resolve_service_by_agent_id(self, *, agent_id: str | None) -> AgentServiceBase:
        """基于 agent_id 索引解析 runtime service，避免误落到默认 runtime。"""
        if not isinstance(agent_id, str) or not agent_id:
            logger.warning("agent runtime resolve failed: missing agent_id")
            raise AgentServiceError(
                code="AGENT_ID_REQUIRED",
                message="agent_id is required",
                status_code=400,
                details=None,
            )
        with self._index_lock:
            runtime_type = self._agent_runtime_index.get(agent_id)
        if runtime_type is None and self._runtime_instance_manager is not None:
            resolved_runtime_type = self._runtime_instance_manager.get_runtime_type(
                agent_id=agent_id,
                runtime_candidates=tuple(self._services.keys()),
            )
            if (
                isinstance(resolved_runtime_type, str)
                and resolved_runtime_type in self._services
            ):
                runtime_type = resolved_runtime_type
                with self._index_lock:
                    self._agent_runtime_index[agent_id] = runtime_type
                logger.info(
                    "agent runtime indexed from instance metadata: agent_id=%s runtime_type=%s",
                    agent_id,
                    runtime_type,
                )
            elif isinstance(resolved_runtime_type, str) and resolved_runtime_type:
                logger.warning(
                    "agent runtime resolve failed: runtime is not registered, agent_id=%s runtime_type=%s",
                    agent_id,
                    resolved_runtime_type,
                )
        if runtime_type is None:
            logger.warning(
                "agent runtime resolve failed: agent runtime is not indexed, agent_id=%s",
                agent_id,
            )
            raise AgentServiceError(
                code="AGENT_RUNTIME_NOT_INDEXED",
                message="agent runtime is not indexed, please start agent first",
                status_code=404,
                details={"agent_id": agent_id},
            )
        return self._require_service(runtime_type)

    def _get_default_service(self) -> AgentServiceBase:
        return self._require_service(self._default_runtime_type)

    def _require_service(self, runtime_type: RuntimeType | None) -> AgentServiceBase:
        if isinstance(runtime_type, str):
            service = self._services.get(runtime_type)
            if service is not None:
                return service
        logger.error(
            "agent runtime service is not configured: runtime=%s", runtime_type
        )
        raise AgentServiceError(
            code="RUNTIME_NOT_SUPPORTED_IN_IMAGE",
            message="runtime unavailable",
            status_code=400,
            details={"runtime_type": runtime_type},
        )
