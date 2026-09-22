from __future__ import annotations

import logging
from typing import Any

from witty_agent_server.application.services.session.base import SessionServiceBase
from witty_agent_server.runtimes.runtime_base import supports_runtime_session_listing

logger = logging.getLogger(__name__)


class OpenClawSessionService(SessionServiceBase):
    """OpenClaw session service。

    通用 create/delete/abort 流程继承自 SessionServiceBase；
    仅覆盖 list_sessions 以优先走 runtime 侧会话列表。
    """

    def list_sessions(self, *, agent_id: str) -> list[dict[str, Any]]:
        runtime_type = self._default_runtime_type
        runtime = (
            self.get_runtime(runtime_type) if isinstance(runtime_type, str) else None
        )
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
