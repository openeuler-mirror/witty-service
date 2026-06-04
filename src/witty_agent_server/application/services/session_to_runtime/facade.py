from __future__ import annotations

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from witty_agent_server.application.services.session_to_runtime.runtime_registry import (
    RuntimeRegistry,
)
from witty_agent_server.application.services.session_to_runtime.base import (
    SessionServiceBase,
)
from witty_agent_server.application.services.session_to_runtime.errors import (
    InvalidSessionConfigError,
    RuntimeNotSupportedError,
    SessionNotFoundServiceError,
)
from witty_agent_server.runtimes.runtime_base import RuntimeBase, RuntimeType

if TYPE_CHECKING:
    from witty_agent_server.application.composition.runtime_instance_manager import (
        RuntimeInstanceManager,
    )


logger = logging.getLogger(__name__)


class SessionFacadeService:
    """统一处理 session runtime 选择与后续路由。"""

    def __init__(
        self,
        *,
        services: Mapping[RuntimeType, SessionServiceBase],
        default_runtime_type: RuntimeType | None = None,
        runtime_instance_manager: RuntimeInstanceManager | None = None,
    ) -> None:
        self._services = dict(services)
        self._runtime_instance_manager = runtime_instance_manager
        self._default_runtime_type = default_runtime_type or next(
            iter(self._services),
            None,
        )
        self.runtime_registry = self._resolve_runtime_registry()
        if self._default_runtime_type is None:
            logger.error("session facade requires at least one runtime service")
            raise ValueError("session facade requires at least one runtime service")

    def create_session(
        self, *, agent_id: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        runtime_type = self._resolve_binding_runtime_type(agent_id=agent_id)
        resolved_config = self._inject_runtime_type(
            config=config,
            runtime_type=runtime_type,
        )
        return self._require_service(runtime_type).create_session(
            agent_id=agent_id,
            config=resolved_config,
        )

    def delete_session(self, *, agent_id: str, session_id: str) -> dict[str, Any]:
        runtime_type = self.require_session_runtime_type(
            agent_id=agent_id, session_id=session_id
        )
        return self._require_service(runtime_type).delete_session(
            agent_id=agent_id,
            session_id=session_id,
        )

    def abort_session(self, *, agent_id: str, session_id: str) -> dict[str, Any]:
        runtime_type = self.require_session_runtime_type(
            agent_id=agent_id, session_id=session_id
        )
        return self._require_service(runtime_type).abort_session(
            agent_id=agent_id,
            session_id=session_id,
        )

    def get_session(self, *, agent_id: str, session_id: str) -> dict[str, Any] | None:
        try:
            runtime_type = self.require_session_runtime_type(
                agent_id=agent_id,
                session_id=session_id,
            )
        except SessionNotFoundServiceError:
            return None
        return self._require_service(runtime_type).get_session(
            agent_id=agent_id,
            session_id=session_id,
        )

    def list_sessions(self, *, agent_id: str) -> list[dict[str, Any]]:
        runtime_type = self._resolve_binding_runtime_type(agent_id=agent_id)
        return self._require_service(runtime_type).list_sessions(agent_id=agent_id)

    def list_events(
        self,
        *,
        agent_id: str,
        session_id: str,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        runtime_type = self.require_session_runtime_type(
            agent_id=agent_id, session_id=session_id
        )
        return self._require_service(runtime_type).list_events(
            agent_id=agent_id,
            session_id=session_id,
            offset=offset,
            limit=limit,
        )

    def append_event(
        self,
        *,
        agent_id: str,
        session_id: str,
        event: dict[str, Any],
    ) -> dict[str, Any]:
        runtime_type = self.require_session_runtime_type(
            agent_id=agent_id, session_id=session_id
        )
        return self._require_service(runtime_type).append_event(
            agent_id=agent_id,
            session_id=session_id,
            event=event,
        )

    def get_runtime(self, runtime_type: str) -> RuntimeBase | None:
        service = self._services.get(cast(RuntimeType, runtime_type))
        if service is None:
            return None
        return service.get_runtime(runtime_type)

    def require_session_runtime_type(
        self, *, agent_id: str, session_id: str
    ) -> RuntimeType:
        for runtime_type, service in self._services.items():
            session = service.get_session(agent_id=agent_id, session_id=session_id)
            if session is None:
                continue
            session_runtime_type = session.get("runtime_type")
            if not isinstance(session_runtime_type, str):
                break
            return cast(RuntimeType, session_runtime_type)
        logger.warning(
            "session not found when resolving runtime: agent_id=%s session_id=%s",
            agent_id,
            session_id,
        )
        raise SessionNotFoundServiceError()

    def _resolve_binding_runtime_type(self, *, agent_id: str) -> RuntimeType:
        """从 RuntimeInstanceManager 缓存查询 runtime_type，未命中则用默认值。"""
        if self._runtime_instance_manager is not None:
            resolved = self._runtime_instance_manager.get_runtime_type(agent_id=agent_id)
            if resolved is not None:
                return cast(RuntimeType, resolved)
        return cast(RuntimeType, self._default_runtime_type)

    def _inject_runtime_type(
        self,
        *,
        config: dict[str, Any],
        runtime_type: RuntimeType,
    ) -> dict[str, Any]:
        resolved_config = dict(config)
        explicit_runtime_type = resolved_config.get("runtime_type")
        if (
            isinstance(explicit_runtime_type, str)
            and explicit_runtime_type != runtime_type
        ):
            logger.error(
                "session runtime conflicts with binding: configured=%s binding=%s",
                explicit_runtime_type,
                runtime_type,
            )
            raise InvalidSessionConfigError("runtime_type conflicts with agent binding")

        runtime_config = resolved_config.get("runtime_config")
        if isinstance(runtime_config, dict):
            enabled_runtimes = [
                name for name, value in runtime_config.items() if value is not None
            ]
            if enabled_runtimes and enabled_runtimes != [runtime_type]:
                logger.error(
                    "session runtime_config conflicts with binding: "
                    "configured=%s binding=%s",
                    enabled_runtimes,
                    runtime_type,
                )
                raise InvalidSessionConfigError(
                    "runtime_config conflicts with agent binding"
                )

        resolved_config["runtime_type"] = runtime_type
        return resolved_config

    def _resolve_runtime_registry(self) -> RuntimeRegistry | None:
        for service in self._services.values():
            runtime_registry = service.runtime_registry
            if runtime_registry is not None:
                return runtime_registry
        return None

    def _require_service(self, runtime_type: RuntimeType) -> SessionServiceBase:
        service = self._services.get(runtime_type)
        if service is None:
            logger.error(
                "session runtime service is not configured: runtime=%s", runtime_type
            )
            raise RuntimeNotSupportedError()
        return service
