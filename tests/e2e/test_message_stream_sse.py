from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from witty_service.adapter.websocket_client_pool import WebSocketClientPool
from witty_service.adapter.websocket_protocol import InboundEvent, OutboundMessage
from witty_service.api.services import ServiceContainer
from witty_service.application.agent_manager import AgentCreateRequest, AgentManager
from witty_service.application.session_manager import SessionManager
from witty_service.main import create_app
from witty_service.persistence.repositories import AgentRecord, SessionRecord
from witty_service.sandbox.base import AdapterEndpoint, SandboxHandle


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
        description: str = "",
        status,
        sandbox_id: str | None = None,
        model_id: str | None = None,
        mcp_server_list: list[str] | None = None,
        last_active_at: Any | None = None,
    ) -> AgentRecord:
        now = datetime.now(UTC)
        agent = AgentRecord(
            id=agent_id,
            name=name,
            description=description,
            sandbox_type=sandbox_type,
            adapter_type=adapter_type,
            status=status,
            sandbox_id=sandbox_id,
            workspace_path=workspace_path,
            idle_timeout_seconds=idle_timeout_seconds,
            model_id=model_id,
            mcp_server_list=list(mcp_server_list or []),
            last_active_at=last_active_at,
            created_at=now,
            updated_at=now,
        )
        self.agents[agent_id] = agent
        return agent

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        return self.agents.get(agent_id)

    def update_agent_status(
        self, agent_id: str, status, updated_at: Any | None = None
    ) -> AgentRecord:
        current = self.agents[agent_id]
        updated = AgentRecord(
            id=current.id,
            name=current.name,
            description=current.description,
            sandbox_type=current.sandbox_type,
            adapter_type=current.adapter_type,
            status=status,
            sandbox_id=current.sandbox_id,
            workspace_path=current.workspace_path,
            idle_timeout_seconds=current.idle_timeout_seconds,
            model_id=current.model_id,
            mcp_server_list=list(current.mcp_server_list),
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
        status: Any = None,
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

    def create_message_event_with_retry(self, **kwargs: Any) -> None:
        return None

    def update_message_content(self, message_id: str, content: str) -> None:
        return None

    def update_message_stream_at(self, message_id: str) -> None:
        return None

    def update_message_status(self, message_id: str, status: Any) -> None:
        return None

    def compact_message_delta_events(self, message_id: str) -> None:
        return None

    def get_first_user_message(self, session_id: str) -> str | None:
        return None

    def get_last_assistant_status(self, session_id: str) -> str | None:
        return None

    def update_session_metadata(self, session_id: str, **kwargs: Any) -> None:
        return None

    def upsert_session(
        self,
        *,
        session_id: str,
        agent_id: str,
        status: str,
        context_initialized: bool = False,
        runtime_type: str | None = None,
        runtime_session_key: str | None = None,
        created_at: datetime | None = None,
        remote_runtime_agent_id: str | None = None,
        **kwargs: Any,
    ) -> SessionRecord:
        session = self.sessions.get(session_id)
        now = datetime.now(UTC)
        if session is None:
            session = SessionRecord(
                id=session_id,
                agent_id=agent_id,
                remote_runtime_agent_id=remote_runtime_agent_id,
                status=status,
                created_at=created_at or now,
                updated_at=now,
                runtime_type=runtime_type,
                runtime_session_key=runtime_session_key,
            )
            self.sessions[session_id] = session
        else:
            session.status = status
            session.updated_at = now
        return session

    def replace_installed_agent_skills_from_runtime(
        self, *, agent_id: str, skills: list[dict[str, Any]]
    ) -> None:
        return None

    def find_stale_generating_messages(
        self, stale_threshold_seconds: int = 30
    ) -> list[Any]:
        return []

    def list_agents_needing_recovery(
        self,
        sandbox_type: str | None = None,
        status_filter: list[Any] | None = None,
    ) -> list[AgentRecord]:
        return []

    def delete_agent(self, agent_id: str) -> None:
        self.agents.pop(agent_id, None)
        self.sandbox_states.pop(agent_id, None)

    def create_session(self, agent_id: str) -> SessionRecord:
        now = datetime.now(UTC)
        session = SessionRecord(
            id="session-1",
            agent_id=agent_id,
            remote_runtime_agent_id="runtime-agent-1",
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> SessionRecord | None:
        return self.sessions.get(session_id)


class FakeWorkspaceStore:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or Path("/tmp")

    def init_workspace(self, agent_id: str) -> Path:
        return self.base_dir / agent_id / "workspace"

    def cleanup_workspace(self, agent_id: str) -> None:
        return None


class FakeSandboxBackend:
    def start(
        self,
        *,
        agent_id: str,
        workspace_path: str,
        env: dict[str, str] | None = None,
        **_: Any,
    ) -> SandboxHandle:
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
        return AdapterEndpoint(
            base_url=f"http://adapter/{handle.sandbox_id}", health_url=None
        )

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


class FakeHttpxResponse:
    def __init__(
        self, status_code: int = 200, json_data: dict[str, Any] | None = None
    ) -> None:
        self.status_code = status_code
        self._json = json_data or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._json


class FakeHttpxClient:
    """create_agent 的 HTTP 集成（健康检查 / agent start / sessions）全部短路。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.base_url = str(kwargs.get("base_url") or "")

    def __enter__(self) -> FakeHttpxClient:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def close(self) -> None:
        return None

    def get(self, path: str, **kwargs: Any) -> FakeHttpxResponse:
        return FakeHttpxResponse(status_code=200)

    def post(self, path: str, **kwargs: Any) -> FakeHttpxResponse:
        if path == "/agent/start":
            return FakeHttpxResponse(
                status_code=200, json_data={"id": "runtime-agent-1"}
            )
        return FakeHttpxResponse(status_code=200, json_data={})


class FakeScheduledTaskService:
    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None


class FakeServices(ServiceContainer):
    def __init__(self, manager: AgentManager, repository: FakeRepository) -> None:
        self.repository = repository
        self.workspace_store = FakeWorkspaceStore()
        self.sandbox_backends = {"local_process": FakeSandboxBackend()}
        self.session_manager = SessionManager(repository)
        self.ws_client_pool = WebSocketClientPool()
        self.scheduled_task_service = FakeScheduledTaskService()
        self._manager = manager

    def get_agent_manager_for_agent(self, agent_id: str) -> AgentManager:
        return self._manager

    async def close(self) -> None:
        return None


def test_message_stream_endpoint_ends_after_completed(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    monkeypatch.setattr("witty_service.config._settings", None)
    monkeypatch.setattr(
        "witty_service.application.agent_manager.httpx.Client",
        FakeHttpxClient,
    )

    repository = FakeRepository()
    session_manager = SessionManager(repository)
    ws_client_pool = WebSocketClientPool()
    manager = AgentManager(
        repository=repository,
        session_manager=session_manager,
        workspace_store=FakeWorkspaceStore(),
        sandbox_backend=FakeSandboxBackend(),
        ws_client_pool=ws_client_pool,
    )
    result = manager.create_agent(
        AgentCreateRequest(
            name="demo",
            sandbox_type="local_process",
            adapter_type="openclaw",
            idle_timeout_seconds=300,
        )
    )
    agent = result.agent
    session = session_manager.create_session(agent.id)
    mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
    mock_ws_client.set_events(
        [
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
        ]
    )

    ws_client_pool.get_client = lambda agent_id, endpoint, factory: mock_ws_client

    client = TestClient(create_app(services=FakeServices(manager, repository)))

    with client.stream(
        "POST",
        f"/agents/{agent.id}/sessions/{session.id}/messages/stream",
        headers={"Authorization": "Bearer test-token"},
        json={"content": "hello"},
    ) as resp:
        chunks = [line for line in resp.iter_lines() if line]

    assert resp.status_code == 200
    assert [m for m in repository.messages if m["role"] == "user"] == [
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
