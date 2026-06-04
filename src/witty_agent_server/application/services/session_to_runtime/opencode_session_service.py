from __future__ import annotations

from witty_agent_server.application.services.session_to_runtime.openclaw_session_service import (
    OpenClawSessionService,
)


class OpenCodeSessionService(OpenClawSessionService):
    """opencode 先复用当前 session 行为，后续再下沉 runtime 差异。"""

