from __future__ import annotations

import logging
from typing import Any

from witty_agent_server.application.services.session_to_runtime.base import SessionServiceBase
from witty_agent_server.application.services.session_to_runtime.errors import (
    RuntimeNotSupportedError,
    SessionServiceError,
)
from witty_agent_server.runtimes.runtime_base import supports_runtime_session_listing


logger = logging.getLogger(__name__)


class OpenClawSessionService(SessionServiceBase):
    """OpenClaw session service，实现显式 agent 归属。"""

    def list_sessions(self, *, agent_id: str) -> list[dict[str, Any]]:
        runtime_type = self._default_runtime_type
        runtime = self.get_runtime(runtime_type) if isinstance(runtime_type, str) else None
        if runtime is None or not supports_runtime_session_listing(runtime):
            logger.info(
                "list_sessions fallback to repository, agent_id=%s runtime_type=%s",
                agent_id,
                runtime_type,
            )
            return super().list_sessions(agent_id=agent_id)
        logger.info(
            "list_sessions use runtime listing, agent_id=%s runtime_type=%s",
            agent_id,
            runtime_type,
        )
        return runtime.list_sessions(agent_id=agent_id)

    def create_session(self, *, agent_id: str, config: dict[str, Any]) -> dict[str, Any]:
        validation = self.validate_create_session(config)
        if not validation.ok:
            from witty_agent_server.application.services.session_to_runtime.errors import (
                InvalidSessionConfigError,
            )

            raise InvalidSessionConfigError(
                validation.message or "invalid session config"
            )

        snapshot = self._resolve_session_snapshot(config=config)
        if self.get_runtime(snapshot.runtime_type) is None:
            raise RuntimeNotSupportedError()

        if self.repository is None:
            raise SessionServiceError(
                code="SESSION_REPOSITORY_NOT_CONFIGURED",
                message="session repository not configured",
                status_code=500,
            )

        session_id = self._generate_session_id()
        runtime_session_key = self._build_runtime_session_key(
            agent_id=agent_id,
            session_id=session_id,
        )
        self._ensure_runtime_session_created(
            runtime_type=snapshot.runtime_type,
            session_key=runtime_session_key,
        )

        session = {
            "id": session_id,
            "agent_id": agent_id,
            "runtime_session_key": runtime_session_key,
            "context_initialized": True,
            **snapshot.model_dump(),
        }
        created_session = self.repository.create(session)
        self._events.setdefault(created_session["id"], [])
        logger.info(
            "session created: agent_id=%s session_id=%s runtime_key=%s runtime_type=%s",
            agent_id,
            created_session["id"],
            created_session["runtime_session_key"],
            created_session["runtime_type"],
        )
        return created_session

    def delete_session(self, *, agent_id: str, session_id: str) -> dict[str, Any]:
        session = self._require_session(agent_id=agent_id, session_id=session_id)
        runtime_type = session.get("runtime_type")
        if not isinstance(runtime_type, str):
            raise RuntimeNotSupportedError()
        runtime_session_key = self._resolve_runtime_session_key(session)

        self._ensure_runtime_session_deleted(
            runtime_type=runtime_type,
            session_key=runtime_session_key,
        )

        if self.repository is None:
            raise SessionServiceError(
                code="SESSION_REPOSITORY_NOT_CONFIGURED",
                message="session repository not configured",
                status_code=500,
            )
        deleted = self.repository.delete(session_id)
        if deleted is None:
            from witty_agent_server.application.services.session_to_runtime.errors import (
                SessionNotFoundServiceError,
            )

            raise SessionNotFoundServiceError()
        self._events.pop(session_id, None)
        logger.info(
            "session deleted: agent_id=%s session_id=%s runtime_key=%s",
            agent_id,
            session_id,
            runtime_session_key,
        )
        return {"id": session_id, "deleted": True}

    def abort_session(self, *, agent_id: str, session_id: str) -> dict[str, Any]:
        session = self._require_session(agent_id=agent_id, session_id=session_id)
        runtime_type = session.get("runtime_type")
        if not isinstance(runtime_type, str):
            raise RuntimeNotSupportedError()
        runtime_session_key = self._resolve_runtime_session_key(session)

        self._ensure_runtime_session_aborted(
            runtime_type=runtime_type,
            session_key=runtime_session_key,
        )
        logger.info(
            "session aborted: agent_id=%s session_id=%s runtime_key=%s",
            agent_id,
            session_id,
            runtime_session_key,
        )
        return {"id": session_id, "aborted": True}
