from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from witty_agent_server.application.models.errors import ValidationResult
from witty_agent_server.application.models.events import (
    EventPagination,
    SessionEvent,
    SessionEventCreate,
    SessionEventPage,
)
from witty_agent_server.application.models.session import SessionConfigSnapshot
from witty_agent_server.application.services.session_to_runtime.errors import (
    InvalidPaginationError,
    InvalidSessionConfigError,
    RuntimeNotSupportedError,
    SessionNotFoundServiceError,
    SessionServiceError,
)
from witty_agent_server.runtimes.runtime_base import RuntimeBase, supports_runtime_lifecycle


logger = logging.getLogger(__name__)


class SessionServiceBase(ABC):
    """Session 服务基础能力，统一 owner 校验和事件管理。"""

    def __init__(
        self,
        runtime_profile_hash: str | None = None,
        runtime_registry: Any = None,
        repository: Any = None,
    ) -> None:
        self.runtime_profile_hash = runtime_profile_hash
        self.runtime_registry = runtime_registry
        self.repository = repository
        self._default_runtime_type: str | None = None
        self._events: dict[str, list[SessionEvent]] = {}

    def validate_create_session(self, config: dict[str, Any]) -> ValidationResult:
        del config
        return ValidationResult(ok=True)

    def register_runtime(self, runtime: RuntimeBase) -> None:
        if self.runtime_registry is None:
            raise ValueError("runtime registry not configured")
        self.runtime_registry.register(runtime)
        if self._default_runtime_type is None:
            self._default_runtime_type = runtime.runtime_type

    def get_runtime(self, runtime_type: str) -> RuntimeBase | None:
        if self.runtime_registry is None:
            return None
        return self.runtime_registry.get(runtime_type)

    @abstractmethod
    def create_session(self, *, agent_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """创建归属于 agent_id 的 session。"""

    @abstractmethod
    def delete_session(self, *, agent_id: str, session_id: str) -> dict[str, Any]:
        """删除归属于 agent_id 的 session。"""

    @abstractmethod
    def abort_session(self, *, agent_id: str, session_id: str) -> dict[str, Any]:
        """中断归属于 agent_id 的 session。"""

    def get_session(self, *, agent_id: str, session_id: str) -> dict[str, Any] | None:
        if self.repository is None:
            return None
        session = self.repository.get(session_id)
        if session is None:
            return None
        self._ensure_session_owner(agent_id=agent_id, session=session)
        return session

    def list_sessions(self, *, agent_id: str) -> list[dict[str, Any]]:
        if self.repository is None:
            return []
        return [
            session
            for session in self.repository.list()
            if session.get("agent_id") == agent_id
        ]

    def append_event(
        self,
        *,
        agent_id: str,
        session_id: str,
        event: SessionEventCreate | dict[str, Any],
    ) -> dict[str, Any]:
        self._require_session(agent_id=agent_id, session_id=session_id)

        payload = (
            event
            if isinstance(event, SessionEventCreate)
            else SessionEventCreate.model_validate(event)
        )
        session_event = SessionEvent.create(
            session_id=session_id,
            type=payload.type,
            source=payload.source,
            payload=payload.payload,
        )
        self._events.setdefault(session_id, []).append(session_event)
        return session_event.model_dump(mode="json")

    def list_events(
        self,
        *,
        agent_id: str,
        session_id: str,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if offset < 0 or limit <= 0:
            raise InvalidPaginationError(offset=offset, limit=limit)

        self._require_session(agent_id=agent_id, session_id=session_id)

        events = self._events.get(session_id, [])
        page = SessionEventPage(
            items=events[offset : offset + limit],
            pagination=EventPagination(
                offset=offset,
                limit=limit,
                total=len(events),
            ),
        )
        return page.model_dump(mode="json")

    def _generate_session_id(self) -> str:
        timestamp = datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
        return f"{timestamp}:{uuid4()}"

    def _build_runtime_session_key(self, *, agent_id: str, session_id: str) -> str:
        return f"agent:{agent_id}:session:{session_id}"

    def _resolve_session_snapshot(
        self, *, config: dict[str, Any] | None = None
    ) -> SessionConfigSnapshot:
        agent_config = config or {}
        common_config = agent_config.get("common_config")
        if not isinstance(common_config, dict):
            common_config = agent_config

        runtime_type = self._resolve_runtime_type(agent_config)
        payload: dict[str, Any] = {
            "runtime_type": runtime_type,
            "prompt": common_config.get("prompt"),
            "runtime_profile_hash": self.runtime_profile_hash,
            "skills": common_config.get("skills", []),
            "mcp": common_config.get("mcp", {}),
            "tools": common_config.get("tools", []),
            "subagents": common_config.get("subagents", []),
        }
        try:
            return SessionConfigSnapshot.model_validate(payload)
        except ValidationError as exc:
            raise InvalidSessionConfigError() from exc

    def _resolve_runtime_type(self, agent_config: dict[str, Any]) -> str | None:
        runtime_config = agent_config.get("runtime_config")
        if isinstance(runtime_config, dict):
            for runtime_type, value in runtime_config.items():
                if value is not None:
                    return runtime_type

        runtime_type = agent_config.get("runtime_type")
        if isinstance(runtime_type, str):
            return runtime_type

        return self._default_runtime_type

    def _require_session(self, *, agent_id: str, session_id: str) -> dict[str, Any]:
        if self.repository is None:
            raise SessionServiceError(
                code="SESSION_REPOSITORY_NOT_CONFIGURED",
                message="session repository not configured",
                status_code=500,
            )
        session = self.repository.get(session_id)
        if session is None:
            raise SessionNotFoundServiceError()
        self._ensure_session_owner(agent_id=agent_id, session=session)
        return session

    def _ensure_session_owner(self, *, agent_id: str, session: dict[str, Any]) -> None:
        session_agent_id = session.get("agent_id")
        if not isinstance(session_agent_id, str) or session_agent_id != agent_id:
            raise SessionNotFoundServiceError()

    def _resolve_runtime_session_key(self, session: dict[str, Any]) -> str:
        runtime_session_key = session.get("runtime_session_key")
        if isinstance(runtime_session_key, str) and runtime_session_key:
            return runtime_session_key
        session_id = session.get("id")
        if isinstance(session_id, str) and session_id:
            return session_id
        raise SessionServiceError(
            code="INVALID_SESSION_RUNTIME_KEY",
            message="invalid session runtime key",
            status_code=500,
        )

    def _ensure_runtime_session_created(
        self, *, runtime_type: str | None, session_key: str
    ) -> None:
        if not isinstance(runtime_type, str):
            return
        runtime = self.get_runtime(runtime_type)
        if runtime is None:
            return
        if not supports_runtime_lifecycle(runtime):
            return
        logger.info(
            "create runtime session: runtime_type=%s session_key=%s",
            runtime_type,
            session_key,
        )
        try:
            runtime.create_session(session_key=session_key)
        except Exception as exc:
            logger.exception(
                "create runtime session failed: runtime_type=%s session_key=%s",
                runtime_type,
                session_key,
            )
            from witty_agent_server.application.services.session_to_runtime.errors import (
                RuntimeSessionCreateFailedError,
            )

            raise RuntimeSessionCreateFailedError() from exc

    def _ensure_runtime_session_deleted(
        self, *, runtime_type: str | None, session_key: str
    ) -> None:
        if not isinstance(runtime_type, str):
            return
        runtime = self.get_runtime(runtime_type)
        if runtime is None:
            return
        if not supports_runtime_lifecycle(runtime):
            return
        logger.info(
            "delete runtime session: runtime_type=%s session_key=%s",
            runtime_type,
            session_key,
        )
        try:
            runtime.delete_session(session_key=session_key)
        except Exception as exc:
            logger.exception(
                "delete runtime session failed: runtime_type=%s session_key=%s",
                runtime_type,
                session_key,
            )
            from witty_agent_server.application.services.session_to_runtime.errors import (
                RuntimeSessionDeleteFailedError,
            )

            raise RuntimeSessionDeleteFailedError() from exc

    def _ensure_runtime_session_aborted(
        self, *, runtime_type: str | None, session_key: str
    ) -> None:
        if not isinstance(runtime_type, str):
            return
        runtime = self.get_runtime(runtime_type)
        if runtime is None:
            return
        if not supports_runtime_lifecycle(runtime):
            return
        logger.info(
            "abort runtime session: runtime_type=%s session_key=%s",
            runtime_type,
            session_key,
        )
        try:
            runtime.abort_session(session_key=session_key)
        except Exception as exc:
            logger.exception(
                "abort runtime session failed: runtime_type=%s session_key=%s",
                runtime_type,
                session_key,
            )
            from witty_agent_server.application.services.session_to_runtime.errors import (
                RuntimeSessionAbortFailedError,
            )

            raise RuntimeSessionAbortFailedError() from exc
