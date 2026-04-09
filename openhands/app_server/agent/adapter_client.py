"""Adapter Client for Agent Middleware Service.

This module provides an HTTP client for communicating with the AgentAdapter
running inside a sandbox. It encapsulates all adapter API calls.

Design reference: Section 6.3.3 (interface mapping) and Section 6.3.4 (Adapter REST API).

The AdapterClient provides:
- start_agent() - Start or resume an agent
- stop_agent() - Stop an agent
- send_message() - Send a message (yields SSE events)
- create_session() - Create a new session
- delete_session() - Delete a session
- get_status() - Get agent status
- update_config() - Update agent config
"""

import json
import logging
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)


class AdapterClient:
    """HTTP client for communicating with AgentAdapter.

    This client is used by AgentManager to communicate with the adapter
    running inside a sandbox. It provides a clean interface for all
    adapter operations.

    Usage:
        client = AdapterClient("http://localhost:8000")
        await client.start_agent(config, workspace_path)
        async for event in client.send_message("Hello", "session-1"):
            print(event)
    """

    def __init__(self, adapter_url: str, httpx_client: Optional[httpx.AsyncClient] = None):
        """Initialize AdapterClient.

        Args:
            adapter_url: Base URL of the adapter server (e.g., "http://localhost:8000")
            httpx_client: Optional httpx client for connection pooling
        """
        self._adapter_url = adapter_url.rstrip("/")
        self._client = httpx_client
        self._owns_client = httpx_client is None

    async def _request(
        self,
        method: str,
        path: str,
        json_data: dict = None,
        timeout: float = 30.0,
    ) -> httpx.Response:
        """Make an HTTP request to the adapter.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            path: API path (e.g., "/api/v1/agent/start")
            json_data: JSON request body
            timeout: Request timeout in seconds

        Returns:
            httpx.Response object

        Raises:
            httpx.HTTPError: If request fails
        """
        url = f"{self._adapter_url}{path}"

        if self._client:
            response = await self._client.request(
                method=method,
                url=url,
                json=json_data,
                timeout=timeout,
            )
        else:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    json=json_data,
                )

        response.raise_for_status()
        return response

    async def start_agent(
        self,
        agent_id: str,
        agent_type: str,
        config: dict,
        workspace_path: str,
        restore: bool = False,
    ) -> dict:
        """Start or resume an agent.

        POST /api/v1/agent/start

        Args:
            agent_id: The agent ID
            agent_type: Agent type (opencode/openclaw/claude-code)
            config: Agent configuration
            workspace_path: Workspace mount path
            restore: Whether to restore from workspace

        Returns:
            {"status": "READY", "sessions": [...]}
        """
        response = await self._request(
            "POST",
            "/api/v1/agent/start",
            json_data={
                "agent_id": agent_id,
                "agent_type": agent_type,
                "config": config,
                "workspace_path": workspace_path,
                "restore": restore,
            },
            timeout=10.0,
        )
        return response.json()

    async def stop_agent(self) -> dict:
        """Stop an agent.

        POST /api/v1/agent/stop

        Returns:
            {"status": "STOPPED"}
        """
        response = await self._request(
            "POST",
            "/api/v1/agent/stop",
            timeout=5.0,
        )
        return response.json()

    async def update_config(self, config: dict) -> dict:
        """Update agent configuration.

        POST /api/v1/agent/config

        Args:
            config: New configuration

        Returns:
            {"status": "updated"}
        """
        response = await self._request(
            "POST",
            "/api/v1/agent/config",
            json_data={"config": config},
            timeout=5.0,
        )
        return response.json()

    async def get_status(self) -> dict:
        """Get agent status.

        GET /api/v1/agent/status

        Returns:
            {"status": "RUNNING", "agent_type": "opencode", ...}
        """
        response = await self._request(
            "GET",
            "/api/v1/agent/status",
            timeout=5.0,
        )
        return response.json()

    async def send_message(
        self,
        content: str,
        session_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a message to an agent.

        POST /api/v1/agent/messages

        This method yields SSE events from the adapter.

        Args:
            content: Message content
            session_id: Session ID

        Yields:
            Event dictionaries: {"type": "thinking", "content": "...", "timestamp": "..."}
        """
        url = f"{self._adapter_url}/api/v1/agent/messages"

        return self.send_message_stream(content=content, session_id=session_id)

    async def send_message_stream(
        self,
        content: str,
        session_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send a message and yield SSE events.

        This is an async generator that yields events as they arrive.

        Args:
            content: Message content
            session_id: Session ID

        Yields:
            Event dictionaries
        """
        url = f"{self._adapter_url}/api/v1/agent/messages"

        if self._client:
            async with self._client.stream(
                "POST",
                url,
                json={"content": content, "session_id": session_id},
                timeout=60.0,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        try:
                            yield json.loads(data)
                        except json.JSONDecodeError:
                            pass
            return

        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream(
                "POST",
                url,
                json={"content": content, "session_id": session_id},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        try:
                            yield json.loads(data)
                        except json.JSONDecodeError:
                            pass

    async def send_message_ws(
        self,
        content: str,
        session_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Send message over WS /api/v1/agent/ws.

        This uses the adapter WebSocket interface and yields streaming events.
        """
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError(
                "websockets dependency is required for send_message_ws"
            ) from exc

        base = self._adapter_url
        if base.startswith("https://"):
            ws_base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            ws_base = "ws://" + base[len("http://"):]
        elif base.startswith("wss://") or base.startswith("ws://"):
            ws_base = base
        else:
            ws_base = f"ws://{base}"

        query = urlencode({"session_id": session_id})
        ws_url = f"{ws_base}/api/v1/agent/ws?{query}"

        async with websockets.connect(ws_url) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "message",
                        "content": content,
                        "session_id": session_id,
                    }
                )
            )

            while True:
                raw = await websocket.recv()
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                yield event
                if event.get("type") in {"done", "error"}:
                    break

    async def create_session(self, session_id: str) -> dict:
        """Create a new session.

        POST /api/v1/agent/sessions

        Args:
            session_id: The session ID to create

        Returns:
            {"id": "session-123", "created_at": "..."}
        """
        response = await self._request(
            "POST",
            "/api/v1/agent/sessions",
            json_data={"session_id": session_id},
            timeout=10.0,
        )
        return response.json()

    async def delete_session(self, session_id: str) -> None:
        """Delete a session.

        DELETE /api/v1/agent/sessions/{session_id}

        Args:
            session_id: The session ID to delete
        """
        await self._request(
            "DELETE",
            f"/api/v1/agent/sessions/{session_id}",
            timeout=10.0,
        )

    async def close(self) -> None:
        """Close the HTTP client if we own it."""
        if self._owns_client and self._client:
            await self._client.aclose()
            self._client = None

    def __repr__(self) -> str:
        return f"AdapterClient(url={self._adapter_url})"


class AdapterClientPool:
    """Pool of AdapterClient instances for different agents.

    This pool manages AdapterClient instances keyed by agent_id,
    allowing reuse of HTTP connections.
    """

    def __init__(self):
        """Initialize the client pool."""
        self._clients: dict[str, AdapterClient] = {}

    def get_client(self, adapter_url: str, agent_id: str) -> AdapterClient:
        """Get or create an AdapterClient for an agent.

        Args:
            adapter_url: Adapter server URL
            agent_id: Agent ID (used as cache key)

        Returns:
            AdapterClient instance
        """
        cache_key = f"{agent_id}:{adapter_url}"
        if cache_key not in self._clients:
            self._clients[cache_key] = AdapterClient(adapter_url)
        return self._clients[cache_key]

    async def close_all(self) -> None:
        """Close all clients in the pool."""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()

    def remove_agent(self, agent_id: str) -> None:
        """Remove all clients for an agent.

        Args:
            agent_id: Agent ID to remove
        """
        keys_to_remove = [k for k in self._clients if k.startswith(f"{agent_id}:")]
        for key in keys_to_remove:
            del self._clients[key]


_global_client_pool: Optional[AdapterClientPool] = None


def get_adapter_client_pool() -> AdapterClientPool:
    """Get the global AdapterClientPool instance.

    Returns:
        Global AdapterClientPool
    """
    global _global_client_pool
    if _global_client_pool is None:
        _global_client_pool = AdapterClientPool()
    return _global_client_pool


def reset_adapter_client_pool() -> None:
    """Reset the global client pool. For testing."""
    global _global_client_pool
    _global_client_pool = None
