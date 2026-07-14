from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from witty_agent_server.application.services.skill.skill_client_port import (
    SkillClientPort,
)


class AgentSkillServiceBase(ABC):
    runtime_type: str

    def __init__(
        self,
        *,
        skill_client: SkillClientPort | None = None,
    ) -> None:
        self._skill_client = skill_client

    def _require_skill_client(self) -> SkillClientPort:
        """返回 skill_client，若子类未提供则抛出清晰错误。

        需要调用 skill_client 的子类应在 __init__中覆写并传入有效实现
        """
        if self._skill_client is None:
            raise RuntimeError(
                f"{type(self).__name__}: skill_client is required but was not "
                f"provided. Override __init__ and pass a SkillClientPort "
                f"implementation."
            )
        return self._skill_client

    @abstractmethod
    def list_skills(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """查询当前 runtime 可用的技能列表。"""

    @abstractmethod
    def install_skill(
        self,
        *,
        agent_id: str | None = None,
        skill_name: str,
        source_type: str | None = None,
        source_path: str | None = None,
        skill_source: str | None = None,
    ) -> dict[str, Any]:
        """安装技能到当前 runtime。source_path 非空时为本地技能目录。"""

    @abstractmethod
    def uninstall_skill(
        self,
        *,
        agent_id: str | None = None,
        skill_name: str,
        source_type: str | None = None,
        source_path: str | None = None,
        runtime_source: str | None = None,
    ) -> dict[str, Any]:
        """卸载当前 runtime 中的技能。"""
