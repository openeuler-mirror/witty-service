from __future__ import annotations

import logging
from typing import Any

from witty_agent_server.application.services.skill.base import AgentSkillServiceBase
from witty_agent_server.application.services.skill.errors import (
    RuntimeSkillsNotSupportedError,
)

logger = logging.getLogger(__name__)


class DshSkillService(AgentSkillServiceBase):
    """dsh runtime 的 skill 服务（最小空实现）。

    满足 ``RuntimeBundle.skill_service`` 必填约束；dsh 的 skill 能力
    走 cordis 配置（后续演进项），MVP 阶段：list 返回空列表，
    install / uninstall 抛不支持。
    """

    runtime_type = "dsh"

    def list_skills(self, *, agent_id: str | None = None) -> dict[str, Any]:
        del agent_id
        logger.info("list_skills requested, runtime_type=%s", self.runtime_type)
        return {
            "runtime_type": self.runtime_type,
            "skills": [],
        }

    def install_skill(
        self,
        *,
        agent_id: str | None = None,
        skill_name: str,
        source_type: str | None = None,
        source_path: str | None = None,
        skill_source: str | None = None,
    ) -> dict[str, Any]:
        del agent_id, source_type, source_path, skill_source
        logger.warning(
            "install_skill not supported, runtime_type=%s skill_name=%s",
            self.runtime_type,
            skill_name,
        )
        raise RuntimeSkillsNotSupportedError(runtime_type=self.runtime_type)

    def uninstall_skill(
        self,
        *,
        agent_id: str | None = None,
        skill_name: str,
        source_type: str | None = None,
        source_path: str | None = None,
        runtime_source: str | None = None,
    ) -> dict[str, Any]:
        del agent_id, source_type, source_path, runtime_source
        logger.warning(
            "uninstall_skill not supported, runtime_type=%s skill_name=%s",
            self.runtime_type,
            skill_name,
        )
        raise RuntimeSkillsNotSupportedError(runtime_type=self.runtime_type)
