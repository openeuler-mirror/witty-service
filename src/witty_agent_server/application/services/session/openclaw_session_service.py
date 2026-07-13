from __future__ import annotations

import logging
from typing import Any

from witty_agent_server.application.services.session.base import SessionServiceBase
from witty_agent_server.application.services.session.errors import (
    RuntimeNotSupportedError,
    SessionServiceError,
)
from witty_agent_server.runtimes.runtime_base import supports_runtime_session_listing


logger = logging.getLogger(__name__)


class OpenClawSessionService(SessionServiceBase):
    """OpenClaw session service。

    通用 create/delete/abort 流程继承自 SessionServiceBase；
    仅覆盖 list_sessions 以优先走 runtime 侧会话列表。
    """

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
            from witty_agent_server.application.services.session.errors import (
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

        # 恢复历史 session 时可传入 session_id，否则自动生成
        session_id = config.get("session_id") or self._generate_session_id()
        runtime_session_key = self._build_runtime_session_key(
            agent_id=agent_id,
            session_id=session_id,
        )
        # restore=True 表示恢复已有 session（服务重启后重填内存缓存），
        # OpenClaw gateway 上的 session 仍存在，无需重新创建
        if not config.get("restore"):
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
