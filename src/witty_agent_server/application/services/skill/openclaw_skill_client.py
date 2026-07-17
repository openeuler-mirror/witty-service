from __future__ import annotations

import logging
from typing import Any

from witty_agent_server.application.services.skill.skill_client_port import (
    SkillClientPort,
)
from witty_agent_server.infra.clients.openclaw_gateway_client import (
    OpenClawGatewayClient,
    OpenClawGatewayClientError,
)

logger = logging.getLogger(__name__)


class OpenClawSkillClient(SkillClientPort):
    """OpenClaw 技能能力客户端，封装 gateway RPC 与 CLI 调用。"""

    def __init__(self, *, gateway_client: OpenClawGatewayClient | None = None) -> None:
        self._gateway_client = gateway_client or OpenClawGatewayClient()

    @property
    def gateway_client(self) -> OpenClawGatewayClient:
        return self._gateway_client

    def get_skills_status(self, *, agent_id: str | None = None) -> dict[str, Any]:
        return self._gateway_client.get_skills_status(agent_id=agent_id)

    def install_skill(
        self,
        *,
        skill_name: str,
        agent_id: str | None = None,
        version: str | None = None,
        force: bool | None = None,
    ) -> dict[str, Any]:
        return self._gateway_client.install_skill(
            skill_name=skill_name,
            agent_id=agent_id,
            version=version,
            force=force,
        )

    def enable_skill(self, *, skill_name: str, agent_id: str | None = None) -> None:
        self._gateway_client.enable_skill(skill_name=skill_name, agent_id=agent_id)

    def uninstall_skill(
        self,
        *,
        skill_name: str,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        return self._gateway_client.uninstall_skill(
            skill_name=skill_name,
            agent_id=agent_id,
        )


__all__ = ["OpenClawSkillClient", "OpenClawGatewayClientError"]
