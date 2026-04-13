from __future__ import annotations
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi.testclient import TestClient

from src.application.agent_manager import AgentCreateRequest, AgentManager
from src.adapter.websocket_client_pool import AdaptorEndpoint, WebSocketClientPool
from src.adapter.websocket_protocol import InboundEvent, OutboundMessage
from src.application.session_manager import SessionManager
from src.main import create_app
from src.persistence.repositories import AgentRecord, SessionRecord
from src.sandbox.base import AdapterEndpoint, SandboxHandle
from src.api.services import ServiceContainer


@dataclass
class FakeSandboxState:
    agent_id: str
    sandbox_payload_json: dict[str, Any]
    adapter_base_url: str
    adapter_ready: bool = True
    last_error: str | None = None

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
        self.agents: dict[str, AgentRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.sandbox_states: dict[str, FakeSandboxState] = {}
        self.messages: list[dict[str, str]] = []

    def create_agent_with_id(
        self,
        *,
        agent_id: str,
        name: str,
        sandbox_type: str,
        adapter_type: str,
        workspace_path: str,
        idle_timeout_seconds: int,
        status,
        sandbox_id: str | None = None,
        has_scheduled_tasks: bool = False,
        last_active_at: Any | None = None,
    ) -> AgentRecord:
        now = datetime.now(UTC)
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
        self.agents[agent_id] = agent
        return agent

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        return self.agents.get(agent_id)

    def update_agent_status(self, agent_id: str, status, updated_at: Any | None = None) -> AgentRecord:
        current = self.agents[agent_id]
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
        self.agents.pop(agent_id, None)
        self.sandbox_states.pop(agent_id, None)

    def create_session(self, agent_id: str) -> SessionRecord:
        now = datetime.now(UTC)
        session = SessionRecord(
            id="session-1",
            agent_id=agent_id,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> SessionRecord | None:
        return self.sessions.get(session_id)


class FakeWorkspaceStore:
    def init_workspace(self, agent_id: str) -> Path:
        return Path("/tmp") / agent_id / "workspace"

    def cleanup_workspace(self, agent_id: str) -> None:
        return None


class FakeSandboxBackend:
    def start(self, *, agent_id: str, workspace_path: str, **_: Any) -> SandboxHandle:
        return SandboxHandle(
            sandbox_id=f"sandbox-{agent_id}",
            agent_id=agent_id,
            workspace_path=workspace_path,
            metadata={},
        )

    def stop(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        return None

    def endpoint(self, handle: SandboxHandle | str, **kwargs: Any) -> AdapterEndpoint:
        assert isinstance(handle, SandboxHandle)
        return AdapterEndpoint(base_url=f"http://adapter/{handle.sandbox_id}", health_url=None)

    def cleanup(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        return None


class StreamingAdapterClient:
    def start(self, *, reload: bool = False) -> dict[str, Any]:
        return {"status": "running"}

    def stop(self) -> dict[str, Any]:
        return {"status": "stopped"}


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


class FakeServices(ServiceContainer):
    def __init__(self, manager: AgentManager, repository: FakeRepository) -> None:
        self.repository = repository
        self.workspace_store = FakeWorkspaceStore()
        self.adapter_client_factory = lambda _: StreamingAdapterClient()
        self.sandbox_backends = {"local_process": FakeSandboxBackend()}
        self.session_manager = SessionManager(repository)
        self.ws_client_pool = WebSocketClientPool()
        self._manager = manager

    def get_agent_manager_for_agent(self, agent_id: str) -> AgentManager:
        return self._manager


def test_message_stream_endpoint_ends_after_completed(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "test-token")

    repository = FakeRepository()
    session_manager = SessionManager(repository)
    ws_client_pool = WebSocketClientPool()
    manager = AgentManager(
        repository=repository,
        session_manager=session_manager,
        workspace_store=FakeWorkspaceStore(),
        sandbox_backend=FakeSandboxBackend(),
        adapter_client_factory=lambda _: StreamingAdapterClient(),
        ws_client_pool=ws_client_pool,
    )
    result = manager.create_agent(
        AgentCreateRequest(
            name="demo",
            sandbox_type="local_process",
            adapter_type="http",
            idle_timeout_seconds=300,
        )
    )
    agent = result.agent
    session = result.default_session
    mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
    mock_ws_client.set_events([
        InboundEvent(
            type="message.delta",
            session_id=session.id,
            runtime_type="openclaw",
            event_id="evt-1",
            ts_ms=100,
            payload={"delta": "hel"},
        ),
        InboundEvent(
            type="message.completed",
            session_id=session.id,
            runtime_type="openclaw",
            event_id="evt-2",
            ts_ms=200,
            payload={},
        ),
        InboundEvent(
            type="message.delta",
            session_id=session.id,
            runtime_type="openclaw",
            event_id="evt-3",
            ts_ms=300,
            payload={"delta": "ignored"},
        ),
    ])

    ws_client_pool.get_client = lambda agent_id, endpoint, factory: mock_ws_client

    client = TestClient(create_app(services=FakeServices(manager, repository)))

    with client.stream(
        "POST",
        f"/api/v1/agents/{agent.id}/sessions/{session.id}/messages/stream",
        headers={"Authorization": "Bearer test-token"},
        json={"content": "hello"},
    ) as resp:
        chunks = [line for line in resp.iter_lines() if line]

    assert resp.status_code == 200
    assert repository.messages == [
        {
            "agent_id": agent.id,
            "session_id": session.id,
            "role": "user",
            "content": "hello",
        }
    ]
    assert mock_ws_client.connect_calls == [session.id]
    assert mock_ws_client.send_calls == [
        {"type": "message.create", "payload": {"message": "hello"}}
    ]
    assert len(chunks) == 2
    first = json.loads(chunks[0].removeprefix("data: "))
    second = json.loads(chunks[1].removeprefix("data: "))
    assert first == {
        "sandbox_type": "local_process",
        "event": {
            "type": "message.delta",
            "session_id": "session-1",
            "runtime_type": "openclaw",
            "event_id": "evt-1",
            "ts_ms": 100,
            "payload": {"delta": "hel"},
        },
    }
    assert second == {
        "sandbox_type": "local_process",
        "event": {
            "type": "message.completed",
            "session_id": "session-1",
            "runtime_type": "openclaw",
            "event_id": "evt-2",
            "ts_ms": 200,
            "payload": {},
        },
    }
