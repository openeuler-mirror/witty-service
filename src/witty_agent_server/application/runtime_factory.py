from __future__ import annotations

from typing import NamedTuple

from witty_agent_server.adapters.runtime_registry import RuntimeRegistry
from witty_agent_server.application.services.agent.opencode_agent_service import (
    OpenCodeAgentService,
)
from witty_agent_server.application.services.agent.opencode_lifecycle_service import (
    OpenCodeLifecycleService,
)
from witty_agent_server.application.services.session.opencode_session_service import (
    OpenCodeSessionService,
)
from witty_agent_server.application.services.skill.opencode_skill_service import (
    OpenCodeSkillService,
)
from witty_agent_server.infra.clients.opencode_client import OpenCodeClient
from witty_agent_server.infra.persistence.in_memory import InMemorySessionRepository
from witty_agent_server.runtimes.opencode_runtime import OpenCodeRuntime


class OpenCodeAssembly(NamedTuple):
    """OpenCode 单 runtime 的装配结果（一个 agent-server = 一份）。"""

    agent_service: OpenCodeAgentService
    session_service: OpenCodeSessionService
    skill_service: OpenCodeSkillService


def create_opencode_bundle(
    *,
    client: OpenCodeClient | None = None,
    lifecycle_service: OpenCodeLifecycleService | None = None,
) -> OpenCodeAssembly:
    """装配 OpenCode runtime 所需的全部服务组件。"""
    shared_client = client or OpenCodeClient()
    shared_lifecycle = lifecycle_service or OpenCodeLifecycleService(
        client=shared_client
    )

    agent_service = OpenCodeAgentService(
        lifecycle_service=shared_lifecycle,
        client=shared_client,
    )

    session_service = OpenCodeSessionService(
        runtime_registry=RuntimeRegistry(),
        repository=InMemorySessionRepository(),
    )
    session_service.register_runtime(OpenCodeRuntime(client=shared_client))

    return OpenCodeAssembly(
        agent_service=agent_service,
        session_service=session_service,
        skill_service=OpenCodeSkillService(),
    )


__all__ = ["OpenCodeAssembly", "create_opencode_bundle"]
