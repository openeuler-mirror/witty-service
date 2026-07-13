from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from witty_agent_server.application.materialization.openclaw_materializer import (
    OpenClawSpecMaterializer,
)
from witty_agent_server.application.runtime_bundle import RuntimeBundle
from witty_agent_server.application.services.agent.openclaw_agent_service import (
    OpenClawAgentService,
)
from witty_agent_server.application.services.agent.openclaw_lifecycle_service import (
    OpenClawLifecycleService,
)
from witty_agent_server.application.services.agent.opencode_agent_service import (
    OpenCodeAgentService,
)
from witty_agent_server.application.services.agent.opencode_lifecycle_service import (
    OpenCodeLifecycleService,
)
from witty_agent_server.application.services.session.openclaw_session_service import (
    OpenClawSessionService,
)
from witty_agent_server.application.services.session.opencode_session_service import (
    OpenCodeSessionService,
)
from witty_agent_server.application.services.skill.openclaw_skill_client import (
    OpenClawSkillClient,
)
from witty_agent_server.application.services.skill.openclaw_skill_service import (
    OpenClawSkillService,
)
from witty_agent_server.application.services.skill.opencode_skill_service import (
    OpenCodeSkillService,
)
from witty_agent_server.adapters.openclaw_adapter import create_openclaw_runtime
from witty_agent_server.adapters.runtime_registry import RuntimeRegistry
from witty_agent_server.infra.clients.opencode_client import OpenCodeClient
from witty_agent_server.infra.persistence.in_memory import InMemorySessionRepository
from witty_agent_server.infra.clients.openclaw_gateway_client import OpenClawGatewayClient
from witty_agent_server.runtimes.opencode_runtime import OpenCodeRuntime
from witty_agent_server.runtimes.runtime_base import RuntimeType


class RuntimeFactory:
    """按 runtime_type 装配 RuntimeBundle。"""

    _BUILDERS: ClassVar[dict[RuntimeType, Callable[..., RuntimeBundle]]] = {}

    @classmethod
    def register(
        cls,
        runtime_type: RuntimeType,
        builder: Callable[..., RuntimeBundle],
    ) -> None:
        """注册一个 runtime bundle builder。"""
        cls._BUILDERS[runtime_type] = builder

    @staticmethod
    def create(runtime_type: RuntimeType, **kwargs: object) -> RuntimeBundle:
        builder = RuntimeFactory._BUILDERS.get(runtime_type)
        if builder is None:
            raise ValueError(f"unsupported runtime_type: {runtime_type}")
        return builder(**kwargs)

    @staticmethod
    def create_openclaw_bundle(
        *,
        gateway_client: OpenClawGatewayClient | None = None,
        lifecycle_service: OpenClawLifecycleService | None = None,
    ) -> RuntimeBundle:
        shared_gateway = gateway_client or OpenClawGatewayClient()
        shared_lifecycle = lifecycle_service or OpenClawLifecycleService()

        agent_service = OpenClawAgentService(
            lifecycle_service=shared_lifecycle,
            gateway_agent_client=shared_gateway,
        )

        runtime_registry = RuntimeRegistry()
        repository = InMemorySessionRepository()
        session_service = OpenClawSessionService(
            runtime_registry=runtime_registry,
            repository=repository,
        )
        runtime = create_openclaw_runtime(client=shared_gateway)
        session_service.register_runtime(runtime)

        skill_service = OpenClawSkillService(
            skill_client=OpenClawSkillClient(gateway_client=shared_gateway),
        )

        return RuntimeBundle(
            runtime_type="openclaw",
            runtime=runtime,
            agent_service=agent_service,
            session_service=session_service,
            skill_service=skill_service,
            lifecycle_service=shared_lifecycle,
            materializer=OpenClawSpecMaterializer(),
        )

    @staticmethod
    def create_opencode_bundle(
        *,
        client: OpenCodeClient | None = None,
        lifecycle_service: OpenCodeLifecycleService | None = None,
    ) -> RuntimeBundle:
        shared_client = client or OpenCodeClient()
        shared_lifecycle = lifecycle_service or OpenCodeLifecycleService(
            client=shared_client
        )

        agent_service = OpenCodeAgentService(
            lifecycle_service=shared_lifecycle,
            client=shared_client,
        )

        runtime_registry = RuntimeRegistry()
        repository = InMemorySessionRepository()
        session_service = OpenCodeSessionService(
            runtime_registry=runtime_registry,
            repository=repository,
        )
        runtime = OpenCodeRuntime(client=shared_client)
        session_service.register_runtime(runtime)

        skill_service = OpenCodeSkillService()

        return RuntimeBundle(
            runtime_type="opencode",
            runtime=runtime,
            agent_service=agent_service,
            session_service=session_service,
            skill_service=skill_service,
            lifecycle_service=shared_lifecycle,
            materializer=None,
        )


RuntimeFactory.register("openclaw", RuntimeFactory.create_openclaw_bundle)
RuntimeFactory.register("opencode", RuntimeFactory.create_opencode_bundle)


__all__ = ["RuntimeFactory"]
