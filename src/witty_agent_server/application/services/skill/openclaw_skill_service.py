from __future__ import annotations

import logging
from typing import Any

from witty_agent_server.application.services.skill.base import AgentSkillServiceBase
from witty_agent_server.application.services.skill.errors import (
    OpenClawSkillsQueryError,
)
from witty_agent_server.infra.ws.openclaw_gateway_client import (
    OpenClawGatewayClientError,
)


logger = logging.getLogger(__name__)


class OpenClawSkillService(AgentSkillServiceBase):
    runtime_type = "openclaw"

    def list_skills(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """查询并返回当前 agent 可用的技能摘要列表。"""
        logger.info(
            "list_skills requested, runtime_type=%s agent_id=%s",
            self.runtime_type,
            agent_id,
        )
        try:
            skills_payload = self._openclaw_client.get_skills_status(agent_id=agent_id)
        except OpenClawGatewayClientError as exc:
            logger.exception(
                "list_skills openclaw rpc failed, runtime_type=%s agent_id=%s code=%s",
                self.runtime_type,
                agent_id,
                exc.code,
            )
            raise OpenClawSkillsQueryError(
                runtime_type=self.runtime_type,
                code=exc.code,
                message=exc.message,
            ) from exc

        logger.info(
            "list_skills success, runtime_type=%s agent_id=%s skill_count=%s",
            self.runtime_type,
            agent_id,
            self._count_eligible_skills(skills_payload),
        )
        return {
            "runtime_type": self.runtime_type,
            "skills": self._normalize_eligible_skills(skills_payload),
        }

    def _normalize_eligible_skills(
        self, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """筛选可用技能，并裁剪为对外暴露的固定字段。"""
        for key in ("skills", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [
                    self._build_skill_summary(item)
                    for item in value
                    if isinstance(item, dict) and item.get("eligible") is True
                ]
        if payload and all(isinstance(key, str) for key in payload):
            return [
                self._build_skill_summary({"name": key, "description": value})
                for key, value in payload.items()
            ]
        return []

    def _count_eligible_skills(self, payload: dict[str, Any]) -> int:
        """统计当前响应中可用技能数量。"""
        return len(self._normalize_eligible_skills(payload))

    def _build_skill_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        """构造对外返回的技能摘要，仅保留约定字段。"""
        return {
            "name": item.get("name"),
            "description": item.get("description"),
            "filePath": item.get("filePath"),
            "source": item.get("source"),
        }
