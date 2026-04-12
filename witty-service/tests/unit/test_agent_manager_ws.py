from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.application.agent_manager import AgentCreateRequest, AgentManager
from src.application.session_manager import SessionManager
from src.adapter.websocket_client_pool import AdaptorEndpoint, WebSocketClientPool
from src.adapter.websocket_protocol import InboundEvent, OutboundMessage
from src.adapter.websocket_client import WebSocketClient
from src.domain.enums import AgentStatus
from src.domain.errors import DomainError
from src.sandbox.base import SandboxHandle


class FakeSandboxState:
    def __init__(
        self,
        agent_id: str,
        sandbox_payload_json: dict[str, object],
        adapter_base_url: str,
        adapter_ready: bool = True,
        last_error: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.sandbox_payload_json = sandbox_payload_json
        self.adapter_base_url = adapter_base_url
        self.adapter_ready = adapter_ready
        self.last_error = last_error

    @property
    def handle(self) -> SandboxHandle:
        return SandboxHandle(
            sandbox_id=self.sandbox_payload_json["sandbox_id"],
            agent_id=self.sandbox_payload_json["agent_id"],
            workspace_path=self.sandbox_payload_json["workspace_path"],
            metadata=self.sandbox_payload_json.get("metadata", {}),
        )


class FakeRepository:
    def __init__(self) -> None:
        self.agent_counter = 0
        self.session_counter = 0
        self.agents: dict[str, Any] = {}
        self.sessions: dict[str, Any] = {}
        self.sandbox_states: dict[str, FakeSandboxState] = {}
        self.messages: list[dict[str, str]] = []
        self.deleted_agents: list[str] = []

    def create_agent_with_id(
        self,
        *,
        agent_id: str,
        name: str,
        sandbox_type: str,
        adapter_type: str,
        workspace_path: str,
        idle_timeout_seconds: int,
        status: AgentStatus = AgentStatus.creating,
        sandbox_id: str | None = None,
        has_scheduled_tasks: bool = False,
        last_active_at: Any | None = None,
    ) -> Any:
        now = datetime.now(UTC)
        from src.persistence.repositories import AgentRecord
        agent = AgentRecord(
            id=agent_id,
            name=name,
            sandbox_type=sandbox_type,
            adapter_type=adapter_type,
            status=status,
            sandbox_id=sandbox_id,
            workspace_path=workspace_path,
            idle_timeout_seconds=idle_timeout_seconds,
            has_scheduled_tasks=has_scheduled_tasks,
            last_active_at=last_active_at,
            created_at=now,
            updated_at=now,
        )
        self.agents[agent.id] = agent
        return agent

    def get_agent(self, agent_id: str) -> Any | None:
        return self.agents.get(agent_id)

    def update_agent_status(
        self,
        agent_id: str,
        status: AgentStatus,
        updated_at: Any | None = None,
    ) -> Any:
        current = self.agents[agent_id]
        from src.persistence.repositories import AgentRecord
        updated = AgentRecord(
            id=current.id,
            name=current.name,
            sandbox_type=current.sandbox_type,
            adapter_type=current.adapter_type,
            status=status,
            sandbox_id=current.sandbox_id,
            workspace_path=current.workspace_path,
            idle_timeout_seconds=current.idle_timeout_seconds,
            has_scheduled_tasks=current.has_scheduled_tasks,
            last_active_at=current.last_active_at,
            created_at=current.created_at,
            updated_at=updated_at or datetime.now(UTC),
        )
        self.agents[agent_id] = updated
        return updated

    def save_sandbox_state(
        self,
        agent_id: str,
        *,
        sandbox_payload_json: dict[str, Any],
        adapter_base_url: str,
        adapter_ready: bool = True,
        last_error: str | None = None,
    ) -> FakeSandboxState:
        state = FakeSandboxState(
            agent_id=agent_id,
            sandbox_payload_json=sandbox_payload_json,
            adapter_base_url=adapter_base_url,
            adapter_ready=adapter_ready,
            last_error=last_error,
        )
        self.sandbox_states[agent_id] = state
        return state

    def get_sandbox_state(self, agent_id: str) -> FakeSandboxState | None:
        return self.sandbox_states.get(agent_id)

    def create_message(
        self,
        *,
        agent_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> str:
        self.messages.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "role": role,
                "content": content,
            }
        )
        return f"message-{len(self.messages)}"

    def delete_agent(self, agent_id: str) -> None:
        self.deleted_agents.append(agent_id)
        self.agents.pop(agent_id, None)
        self.sandbox_states.pop(agent_id, None)

    def create_session(self, agent_id: str) -> Any:
        self.session_counter += 1
        now = datetime.now(UTC)
        from src.persistence.repositories import SessionRecord
        session = SessionRecord(
            id=f"session-{self.session_counter}",
            agent_id=agent_id,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> Any | None:
        return self.sessions.get(session_id)


class FakeWorkspaceStore:
    def init_workspace(self, agent_id: str) -> Path:
        return Path("/tmp") / agent_id / "workspace"

    def cleanup_workspace(self, agent_id: str) -> None:
        pass


class FakeSandboxBackend:
    def __init__(self) -> None:
        self.handles: dict[str, Any] = {}

    def start(self, *, agent_id: str, workspace_path: str, **_: Any) -> Any:
        from src.sandbox.base import SandboxHandle
        handle = SandboxHandle(
            sandbox_id=f"sandbox-{agent_id}",
            agent_id=agent_id,
            workspace_path=workspace_path,
            metadata={},
        )
        self.handles[agent_id] = handle
        return handle

    def stop(self, handle: Any, **_: Any) -> None:
        pass

    def endpoint(self, handle: Any, **_: Any) -> Any:
        from src.sandbox.base import AdapterEndpoint
        return AdapterEndpoint(base_url=f"http://adapter/{handle.sandbox_id}", health_url=None)

    def cleanup(self, handle: Any, **_: Any) -> None:
        pass


class FakeAdapterClient:
    def start(self, *, reload: bool = False) -> dict[str, Any]:
        return {"status": "running"}

    def stop(self) -> dict[str, Any]:
        return {"status": "stopped"}

    def send_message_stream(self, session_id: str, message: str) -> Any:
        return iter([{"type": "delta", "delta": "hello"}])


class MockWebSocketClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.is_connected = False
        self.connect_calls: list[str] = []
        self.send_calls: list[OutboundMessage] = []
        self._events: list[InboundEvent] = []

    async def connect(self, session_id: str) -> None:
        self.connect_calls.append(session_id)
        self.is_connected = True

    async def send(self, message: OutboundMessage) -> None:
        self.send_calls.append(message)

    def set_events(self, events: list[InboundEvent]) -> None:
        self._events = events

    def recv(self) -> AsyncIterator[InboundEvent]:
        async def gen():
            for event in self._events:
                yield event
        return gen()


def _make_ws_manager(
    ws_client_pool: WebSocketClientPool | None = None,
):
    repository = FakeRepository()
    workspace_store = FakeWorkspaceStore()
    sandbox_backend = FakeSandboxBackend()
    adapter_factory = MagicMock(return_value=FakeAdapterClient())
    session_manager = SessionManager(repository)

    if ws_client_pool is None:
        ws_client_pool = WebSocketClientPool()

    manager = AgentManager(
        repository=repository,
        session_manager=session_manager,
        workspace_store=workspace_store,
        sandbox_backend=sandbox_backend,
        adapter_client_factory=adapter_factory,
        ws_client_pool=ws_client_pool,
    )

    request = AgentCreateRequest(
        name="demo",
        sandbox_type="local_process",
        adapter_type="http",
        idle_timeout_seconds=300,
    )
    return (
        manager,
        request,
        repository,
        workspace_store,
        sandbox_backend,
        ws_client_pool,
    )


def _create_agent_with_sandbox(manager: AgentManager, request: AgentCreateRequest) -> tuple[Any, Any]:
    """Helper to create an agent and set up sandbox state"""
    result = manager.create_agent(request)
    agent = result.agent
    session = result.default_session
    return agent, session


@pytest.mark.asyncio
async def test_send_message_via_websocket_client():
    """Test that send_message uses WebSocket client to send and receive messages"""
    manager, request, repository, _, sandbox_backend, ws_client_pool = _make_ws_manager()

    agent, session = _create_agent_with_sandbox(manager, request)

    # Create mock WS client
    mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
    mock_ws_client.set_events([
        InboundEvent(
            type="message.delta",
            session_id=session.id,
            runtime_type="local_process",
            event_id="evt-1",
            ts_ms=1000,
            payload={"delta": "hello"},
        ),
        InboundEvent(
            type="message.completed",
            session_id=session.id,
            runtime_type="local_process",
            event_id="evt-2",
            ts_ms=2000,
            payload={},
        ),
    ])

    # Patch get_client to return our mock
    with patch.object(
        ws_client_pool,
        "get_client",
        return_value=mock_ws_client,
    ):
        events = await manager.send_message(agent.id, session.id, "hello from user")

    # Verify message was stored in repository
    assert repository.messages == [
        {
            "agent_id": agent.id,
            "session_id": session.id,
            "role": "user",
            "content": "hello from user",
        }
    ]

    # Verify send was called with message.create
    assert len(mock_ws_client.send_calls) == 1
    assert mock_ws_client.send_calls[0]["type"] == "message.create"
    assert mock_ws_client.send_calls[0]["payload"] == {"message": "hello from user"}

    # Verify events were returned
    assert len(events) == 2
    assert events[0]["type"] == "message.delta"
    assert events[1]["type"] == "message.completed"


@pytest.mark.asyncio
async def test_send_message_connects_when_not_connected():
    """Test that send_message connects WebSocket if not connected"""
    manager, request, repository, _, _, ws_client_pool = _make_ws_manager()

    agent, session = _create_agent_with_sandbox(manager, request)

    mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
    mock_ws_client.set_events([
        InboundEvent(
            type="message.completed",
            session_id=session.id,
            runtime_type="local_process",
            event_id="evt-1",
            ts_ms=1000,
            payload={},
        ),
    ])

    with patch.object(
        ws_client_pool,
        "get_client",
        return_value=mock_ws_client,
    ):
        await manager.send_message(agent.id, session.id, "hello")

    # Verify connect was called since client wasn't connected
    assert mock_ws_client.connect_calls == [session.id]


@pytest.mark.asyncio
async def test_send_message_auto_resumes_paused_agent():
    """Test that send_message resumes paused agent before sending via WS"""
    manager, request, repository, _, sandbox_backend, ws_client_pool = _make_ws_manager()

    agent, session = _create_agent_with_sandbox(manager, request)
    manager.pause_agent(agent.id)

    mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
    mock_ws_client.set_events([
        InboundEvent(
            type="message.completed",
            session_id=session.id,
            runtime_type="local_process",
            event_id="evt-1",
            ts_ms=1000,
            payload={},
        ),
    ])

    with patch.object(
        ws_client_pool,
        "get_client",
        return_value=mock_ws_client,
    ):
        events = await manager.send_message(agent.id, session.id, "hello")

    # Verify agent was resumed
    assert repository.get_agent(agent.id).status is AgentStatus.running
    # Verify message was sent via WS
    assert len(mock_ws_client.send_calls) == 1


@pytest.mark.asyncio
async def test_send_message_rejects_non_running_agent():
    """Test that send_message raises error for non-running/non-paused agent"""
    manager, request, repository, _, _, _ = _make_ws_manager()

    agent, session = _create_agent_with_sandbox(manager, request)

    # Manually set status to something other than running/paused
    repository.update_agent_status(agent.id, AgentStatus.error)

    with pytest.raises(DomainError) as exc_info:
        await manager.send_message(agent.id, session.id, "hello")

    assert exc_info.value.code == "AGENT_NOT_RUNNING"


def test_get_adaptor_endpoint_converts_http_to_ws():
    """Test that _get_adaptor_endpoint converts http/https to ws/wss"""
    manager, request, repository, _, _, ws_client_pool = _make_ws_manager()

    agent, session = _create_agent_with_sandbox(manager, request)

    # Set adapter_base_url to https
    repository.save_sandbox_state(
        agent.id,
        sandbox_payload_json={"sandbox_id": "sandbox-test", "agent_id": agent.id, "workspace_path": "/tmp"},
        adapter_base_url="https://adapter.example.com",
    )

    endpoint = manager._get_adaptor_endpoint(agent.id, session.id)

    assert endpoint.base_url == "wss://adapter.example.com"
    assert endpoint.session_id == session.id
    assert endpoint.sandbox_type == "local_process"


def test_get_adaptor_endpoint_converts_http_without_scheme():
    """Test that _get_adaptor_endpoint handles http:// URLs"""
    manager, request, repository, _, _, ws_client_pool = _make_ws_manager()

    agent, session = _create_agent_with_sandbox(manager, request)

    repository.save_sandbox_state(
        agent.id,
        sandbox_payload_json={"sandbox_id": "sandbox-test", "agent_id": agent.id, "workspace_path": "/tmp"},
        adapter_base_url="http://adapter.local",
    )

    endpoint = manager._get_adaptor_endpoint(agent.id, session.id)

    assert endpoint.base_url == "ws://adapter.local"
    assert endpoint.session_id == session.id
