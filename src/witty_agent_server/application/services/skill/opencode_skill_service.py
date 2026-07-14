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

    def install_skill(
        self,
        *,
        agent_id: str | None = None,
        skill_name: str,
        source_type: str | None = None,
        source_path: str | None = None,
        skill_source: str | None = None,
    ) -> dict[str, Any]:
        del agent_id, skill_name, source_type, source_path, skill_source
        raise RuntimeSkillsNotSupportedError(runtime_type=self.runtime_type)

    def uninstall_skill(
        self,
        *,
        agent_id: str | None = None,
        skill_name: str,
        source_type: str | None = None,
        source_path: str | None = None,
    ) -> dict[str, Any]:
        del agent_id, skill_name, source_type, source_path
        raise RuntimeSkillsNotSupportedError(runtime_type=self.runtime_type)
