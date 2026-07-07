from witty_agent_server.infra.clients.openclaw_gateway_client import (
    OpenClawGatewayClient,
)
from witty_agent_server.runtimes.openclaw_gateway_runtime import OpenClawGatewayRuntime


class OpenClawAdapter:
    def __init__(
        self,
        *,
        client: OpenClawGatewayClient | None = None,
        runtime: OpenClawGatewayRuntime | None = None,
    ) -> None:
        self._client = client or OpenClawGatewayClient()
        self._runtime = runtime or OpenClawGatewayRuntime(client=self._client)

    @property
    def runtime(self) -> OpenClawGatewayRuntime:
        return self._runtime

    def probe(self) -> tuple[bool, str | None]:
        return True, None


def create_openclaw_runtime(
    *,
    client: OpenClawGatewayClient | None = None,
    ws_url: str | None = None,
    gateway_token: str | None = None,
    profile: str | None = None,
    gateway_port: int | None = None,
) -> OpenClawGatewayRuntime:
    resolved_client = client or OpenClawGatewayClient(
        url=ws_url,
        token=gateway_token,
        profile=profile,
        gateway_port=gateway_port,
    )
    return OpenClawGatewayRuntime(client=resolved_client)


def create_openclaw_adapter(
    *,
    ws_url: str | None = None,
    gateway_token: str | None = None,
    profile: str | None = None,
    gateway_port: int | None = None,
) -> OpenClawAdapter:
    client = OpenClawGatewayClient(
        url=ws_url,
        token=gateway_token,
        profile=profile,
        gateway_port=gateway_port,
    )
    return OpenClawAdapter(
        client=client,
        runtime=OpenClawGatewayRuntime(client=client),
    )