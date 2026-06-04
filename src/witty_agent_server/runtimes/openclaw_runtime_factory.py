from __future__ import annotations

from typing import TYPE_CHECKING

from witty_agent_server.infra.ws.openclaw_gateway_client import (
    DEFAULT_GATEWAY_WS_URL,
    OpenClawGatewayClient,
)
from witty_agent_server.runtimes.openclaw_gateway_runtime import OpenClawGatewayRuntime

if TYPE_CHECKING:
    from witty_agent_server.application.composition.runtime_instance_manager import (
        RuntimeInstanceManager,
    )


def create_openclaw_runtime(
    *,
    ws_url: str = DEFAULT_GATEWAY_WS_URL,
    gateway_token: str | None = None,
    runtime_instance_manager: RuntimeInstanceManager | None = None,
) -> OpenClawGatewayRuntime:
    """创建 OpenClaw runtime 实例，供 session 装配层复用。"""
    return OpenClawGatewayRuntime(
        client=OpenClawGatewayClient(
            url=ws_url,
            token=gateway_token,
        ),
        runtime_instance_manager=runtime_instance_manager,
    )
