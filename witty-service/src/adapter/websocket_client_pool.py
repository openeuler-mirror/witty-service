from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from src.adapter.websocket_client import WebSocketClient

@dataclass(frozen=True)
class AdaptorEndpoint:
    base_url: str
    session_id: str
    sandbox_type: str

class WebSocketClientPool:
    def __init__(self) -> None:
        self._clients: dict[str, WebSocketClient] = {}

    def get_client(
        self,
        agent_id: str,
        endpoint: AdaptorEndpoint,
        factory: Callable[[str], WebSocketClient],
    ) -> WebSocketClient:
        if agent_id not in self._clients:
            self._clients[agent_id] = factory(endpoint.base_url)
        return self._clients[agent_id]

    def remove_client(self, agent_id: str) -> None:
        self._clients.pop(agent_id, None)

    async def close_all(self) -> None:
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
