from __future__ import annotations

from typing import Any

from witty_agent_server.application.services.skill.base import AgentSkillServiceBase
from witty_agent_server.application.services.skill.errors import (
    RuntimeSkillsNotSupportedError,
)


class OpenCodeSkillService(AgentSkillServiceBase):
    runtime_type = "opencode"

    def list_skills(self, *, agent_id: str | None = None) -> dict[str, Any]:
        del agent_id
        raise RuntimeSkillsNotSupportedError(runtime_type=self.runtime_type)
