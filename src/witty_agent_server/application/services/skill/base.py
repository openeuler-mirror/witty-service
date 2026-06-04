from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from witty_agent_server.infra.ws.openclaw_gateway_client import OpenClawGatewayClient


class AgentSkillServiceBase(ABC):
    runtime_type: str

    def __init__(
        self,
        *,
        openclaw_client: OpenClawGatewayClient | None = None,
    ) -> None:
        self._openclaw_client = openclaw_client or OpenClawGatewayClient()

    @abstractmethod
    def list_skills(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """查询当前 runtime 可用的技能列表。"""

