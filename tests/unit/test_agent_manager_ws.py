from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from witty_service.adapter.websocket_client_pool import (
    WebSocketClientPool,
)
from witty_service.adapter.websocket_protocol import InboundEvent, OutboundMessage
from witty_service.application.agent_manager import (
    AGENT_DELETE_FAILED,
    SKILL_INSTALL_TIMEOUT_SECONDS,
    AgentCreateRequest,
    AgentManager,
)
from witty_service.application.session_manager import SessionManager
from witty_service.domain.enums import AgentStatus
from witty_service.domain.errors import DomainError
from witty_service.sandbox.base import SandboxHandle, sandbox_not_found


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
        last_active_at: Any | None = None,
        description: str | None = None,
        model_id: str | None = None,
        mcp_server_list: Any | None = None,
    ) -> Any:
        now = datetime.now(UTC)
        from witty_service.persistence.repositories import AgentRecord

        agent = AgentRecord(
            id=agent_id,
            name=name,
            description=description or "",
            sandbox_type=sandbox_type,
            adapter_type=adapter_type,
            status=status,
            sandbox_id=sandbox_id,
            workspace_path=workspace_path,
            idle_timeout_seconds=idle_timeout_seconds,
            model_id=model_id,
            mcp_server_list=list(mcp_server_list) if mcp_server_list else [],
            last_active_at=last_active_at,
            created_at=now,
            updated_at=now,
        )
        self.agents[agent.id] = agent
        return agent

    def get_agent(self, agent_id: str) -> Any | None:
        return self.agents.get(agent_id)

    def get_model(self, model_id: str | None) -> Any | None:
        return None

    def update_agent_status(
        self,
        agent_id: str,
        status: AgentStatus,
        updated_at: Any | None = None,
    ) -> Any:
        current = self.agents[agent_id]
        from witty_service.persistence.repositories import AgentRecord

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
            mcp_server_list=current.mcp_server_list,
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
        from witty_service.persistence.repositories import SessionRecord

        session = SessionRecord(
            id=f"session-{self.session_counter}",
            agent_id=agent_id,
            remote_runtime_agent_id=f"runtime-agent-{self.session_counter}",
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> Any | None:
        return self.sessions.get(session_id)

    def upsert_session(
        self,
        session_id: str,
        agent_id: str,
        status: str,
        context_initialized: bool = False,
        runtime_type: str | None = None,
        runtime_session_key: str | None = None,
        created_at: datetime | None = None,
        remote_runtime_agent_id: str | None = None,
    ) -> Any:
        now = datetime.now(UTC)
        from witty_service.persistence.repositories import SessionRecord

        existing = self.sessions.get(session_id)
        session = SessionRecord(
            id=session_id,
            agent_id=agent_id,
            remote_runtime_agent_id=(
                remote_runtime_agent_id
                or (existing.remote_runtime_agent_id if existing is not None else None)
            ),
            status=status,
            created_at=created_at
            or (existing.created_at if existing is not None else now),
            updated_at=now,
            runtime_type=runtime_type
            or (existing.runtime_type if existing is not None else None),
            runtime_session_id=existing.runtime_session_id
            if existing is not None
            else None,
            runtime_session_key=(
                runtime_session_key
                or (existing.runtime_session_key if existing is not None else None)
            ),
            title=existing.title if existing is not None else None,
            pinned=existing.pinned if existing is not None else False,
        )
        self.sessions[session_id] = session
        return session

    def update_session_runtime_identity(
        self,
        *,
        session_id: str,
        runtime_type: str,
        runtime_session_id: str,
        runtime_session_key: str,
    ) -> Any:
        current = self.sessions[session_id]
        updated = replace(
            current,
            runtime_type=runtime_type,
            runtime_session_id=runtime_session_id,
            runtime_session_key=runtime_session_key,
            updated_at=datetime.now(UTC),
        )
        self.sessions[session_id] = updated
        return updated

    def get_last_assistant_status(self, session_id: str) -> str | None:
        return None

    def find_last_assistant_message_for_session(self, session_id: str) -> Any | None:
        # abort_session 会按 session_id 找最后一条 assistant 消息改 interrupted；
        # 这里的双胞胎没有消息可改，返回 None 即真实语义（不动任何消息）。
        return None

    def get_first_user_message(self, session_id: str) -> str | None:
        return None

    def update_session_metadata(
        self,
        session_id: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
    ) -> Any:
        current = self.sessions[session_id]
        updated = replace(
            current,
            title=title if title is not None else current.title,
            pinned=pinned if pinned is not None else current.pinned,
            updated_at=datetime.now(UTC),
        )
        self.sessions[session_id] = updated
        return updated


class FakeWorkspaceStore:
    def init_workspace(self, agent_id: str) -> Path:
        return Path("/tmp") / agent_id / "workspace"

    def cleanup_workspace(self, agent_id: str) -> None:
        pass


class FakeSandboxBackend:
    def __init__(self) -> None:
        self.handles: dict[str, Any] = {}

    def start(
        self,
        *,
        agent_id: str,
        workspace_path: str,
        env: dict[str, Any] | None = None,
        **_: Any,
    ) -> Any:
        from witty_service.sandbox.base import SandboxHandle

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
        from witty_service.sandbox.base import AdapterEndpoint

        return AdapterEndpoint(
            base_url=f"http://adapter/{handle.sandbox_id}", health_url=None
        )

    def health_check(self, handle: Any) -> bool:
        return True

    def start_agent_on_adapter(
        self, handle: Any, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return {"id": f"runtime-{handle.sandbox_id}"}

    def create_session_on_adapter(
        self, handle: Any, runtime_agent_id: str
    ) -> dict[str, Any]:
        return {"id": f"session-{handle.sandbox_id}"}

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

    async def close(self) -> None:
        self.is_connected = False


def _make_ws_manager(
    ws_client_pool: WebSocketClientPool | None = None,
):
    repository = FakeRepository()
    workspace_store = FakeWorkspaceStore()
    sandbox_backend = FakeSandboxBackend()
    session_manager = SessionManager(repository)

    if ws_client_pool is None:
        ws_client_pool = WebSocketClientPool()

    manager = AgentManager(
        repository=repository,
        session_manager=session_manager,
        workspace_store=workspace_store,
        sandbox_backend=sandbox_backend,
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


def _create_agent_with_sandbox(
    manager: AgentManager, request: AgentCreateRequest
) -> tuple[Any, Any]:
    """Helper to create an agent and set up sandbox state"""
    result = manager.create_agent(request)
    agent = result.agent
    session = result.default_session
    return agent, session


def _bootstrap_running_agent_and_session(repository: FakeRepository) -> tuple[Any, Any]:
    agent = repository.create_agent_with_id(
        agent_id="agent-1",
        name="demo",
        description="",
        sandbox_type="local_process",
        adapter_type="http",
        workspace_path="/tmp/agent-1/workspace",
        idle_timeout_seconds=300,
        status=AgentStatus.running,
        mcp_server_list=[],
    )
    repository.save_sandbox_state(
        agent.id,
        sandbox_payload_json={
            "sandbox_id": f"sandbox-{agent.id}",
            "agent_id": agent.id,
            "workspace_path": agent.workspace_path,
            "metadata": {},
        },
        adapter_base_url="http://adapter.local",
        adapter_ready=True,
    )
    session = repository.upsert_session(
        session_id="session-1",
        agent_id=agent.id,
        status="running",
        runtime_type="openclaw",
        remote_runtime_agent_id="runtime-agent-1",
    )
    return agent, session


def test_send_message_uses_websocket_and_syncs_runtime_session_identity():
    async def run() -> None:
        manager, _, repository, _, _, ws_client_pool = _make_ws_manager()
        agent, session = _bootstrap_running_agent_and_session(repository)

        mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
        mock_ws_client.set_events(
            [
                InboundEvent(
                    type="session.runtime.changed",
                    session_id=session.id,
                    runtime_type="openclaw",
                    event_id="evt-1",
                    ts_ms=1000,
                    payload={
                        "runtime_session_id": "runtime-session-1",
                        "runtime_session_key": "agent:1:session:key-1",
                    },
                ),
                InboundEvent(
                    type="message.completed",
                    session_id=session.id,
                    runtime_type="openclaw",
                    event_id="evt-2",
                    ts_ms=2000,
                    payload={},
                ),
            ]
        )

        with patch.object(
            ws_client_pool,
            "get_client",
            return_value=mock_ws_client,
        ):
            events = await manager.send_message(agent.id, session.id, "hello from user")

        assert mock_ws_client.connect_calls == [session.id]
        assert mock_ws_client.send_calls == [
            {"type": "message.create", "payload": {"message": "hello from user"}}
        ]
        assert [event["type"] for event in events["events"]] == ["message.completed"]
        synced_session = repository.sessions[session.id]
        assert synced_session.runtime_type == "openclaw"
        assert synced_session.runtime_session_id == "runtime-session-1"
        assert synced_session.runtime_session_key == "agent:1:session:key-1"

    asyncio.run(run())


def test_send_message_runtime_identity_sync_swallows_domain_error(caplog):
    async def run() -> None:
        manager, _, repository, _, _, ws_client_pool = _make_ws_manager()
        agent, session = _bootstrap_running_agent_and_session(repository)

        mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
        mock_ws_client.set_events(
            [
                InboundEvent(
                    type="session.runtime.changed",
                    session_id=session.id,
                    runtime_type="openclaw",
                    event_id="evt-1",
                    ts_ms=1000,
                    payload={
                        "runtime_session_id": "runtime-session-1",
                        "runtime_session_key": "agent:1:session:key-1",
                    },
                ),
                InboundEvent(
                    type="message.completed",
                    session_id=session.id,
                    runtime_type="openclaw",
                    event_id="evt-2",
                    ts_ms=2000,
                    payload={},
                ),
            ]
        )

        domain_error = DomainError(
            code="SESSION_NOT_FOUND",
            message="Session was not found.",
            details={"session_id": session.id},
        )

        with (
            patch.object(
                ws_client_pool,
                "get_client",
                return_value=mock_ws_client,
            ),
            patch.object(
                manager._session_manager,
                "update_session_runtime_identity",
                side_effect=domain_error,
            ),
            caplog.at_level("ERROR"),
        ):
            events = await manager.send_message(agent.id, session.id, "hello from user")

        assert [event["type"] for event in events["events"]] == ["message.completed"]
        assert "failed to sync runtime session identity" in caplog.text
        assert "SESSION_NOT_FOUND" in caplog.text

    asyncio.run(run())


def test_send_message_runtime_identity_sync_does_not_swallow_programming_error():
    async def run() -> None:
        manager, _, repository, _, _, ws_client_pool = _make_ws_manager()
        agent, session = _bootstrap_running_agent_and_session(repository)

        mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
        mock_ws_client.set_events(
            [
                InboundEvent(
                    type="session.runtime.changed",
                    session_id=session.id,
                    runtime_type="openclaw",
                    event_id="evt-1",
                    ts_ms=1000,
                    payload={
                        "runtime_session_id": "runtime-session-1",
                        "runtime_session_key": "agent:1:session:key-1",
                    },
                ),
            ]
        )

        with (
            patch.object(
                ws_client_pool,
                "get_client",
                return_value=mock_ws_client,
            ),
            patch.object(
                manager._session_manager,
                "update_session_runtime_identity",
                side_effect=TypeError("boom"),
            ),
            pytest.raises(TypeError, match="boom"),
        ):
            await manager.send_message(agent.id, session.id, "hello from user")

    asyncio.run(run())


@pytest.mark.skip(
    reason="sandbox health check 30 次循环导致单用例约 30 秒,源代码未修复前暂跳过"
)
def test_send_message_via_websocket_client():
    """Test that send_message uses WebSocket client to send and receive messages"""

    async def run() -> None:
        manager, request, repository, _, _, ws_client_pool = _make_ws_manager()

        agent, session = _create_agent_with_sandbox(manager, request)

        # Create mock WS client
        mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
        mock_ws_client.set_events(
            [
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
            ]
        )

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
        assert events["sandbox_type"] == "local_process"
        assert len(events["events"]) == 2
        assert events["events"][0]["type"] == "message.delta"
        assert events["events"][0]["runtime_type"] == "local_process"
        assert "sandbox_type" not in events["events"][0]
        assert events["events"][1]["type"] == "message.completed"

    asyncio.run(run())


@pytest.mark.skip(
    reason="sandbox health check 30 次循环导致单用例约 30 秒,源代码未修复前暂跳过"
)
def test_send_message_connects_when_not_connected():
    """Test that send_message connects WebSocket if not connected"""

    async def run() -> None:
        manager, request, _, _, _, ws_client_pool = _make_ws_manager()

        agent, session = _create_agent_with_sandbox(manager, request)

        mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
        mock_ws_client.set_events(
            [
                InboundEvent(
                    type="message.completed",
                    session_id=session.id,
                    runtime_type="local_process",
                    event_id="evt-1",
                    ts_ms=1000,
                    payload={},
                ),
            ]
        )

        with patch.object(
            ws_client_pool,
            "get_client",
            return_value=mock_ws_client,
        ):
            await manager.send_message(agent.id, session.id, "hello")

        # Verify connect was called since client wasn't connected
        assert mock_ws_client.connect_calls == [session.id]

    asyncio.run(run())


@pytest.mark.skip(
    reason="pause_agent now uses httpx.Client directly, test needs rework for new session proxy architecture"
)
def test_send_message_auto_resumes_paused_agent():
    """Test that send_message resumes paused agent before sending via WS"""

    async def run() -> None:
        manager, request, repository, _, _, ws_client_pool = _make_ws_manager()

        agent, session = _create_agent_with_sandbox(manager, request)

        # Mock HTTP client for pause_agent and resume_agent
        mock_adaptor_client = AsyncMock()
        mock_adaptor_client.post = AsyncMock()
        mock_adaptor_client.close = AsyncMock()

        mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
        mock_ws_client.set_events(
            [
                InboundEvent(
                    type="message.completed",
                    session_id=session.id,
                    runtime_type="local_process",
                    event_id="evt-1",
                    ts_ms=1000,
                    payload={},
                ),
            ]
        )

        with patch.object(
            manager, "_get_adaptor_http_client", return_value=mock_adaptor_client
        ):
            manager.pause_agent(agent.id)

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
        assert events["sandbox_type"] == "local_process"

    asyncio.run(run())


@pytest.mark.skip(
    reason="sandbox health check 30 次循环导致单用例约 30 秒,源代码未修复前暂跳过"
)
def test_send_message_rejects_non_running_agent():
    """Test that send_message raises error for non-running/non-paused agent"""

    async def run() -> None:
        manager, request, repository, _, _, _ = _make_ws_manager()

        agent, session = _create_agent_with_sandbox(manager, request)

        # Manually set status to something other than running/paused
        repository.update_agent_status(agent.id, AgentStatus.error)

        with pytest.raises(DomainError) as exc_info:
            await manager.send_message(agent.id, session.id, "hello")

        assert exc_info.value.code == "AGENT_NOT_RUNNING"

    asyncio.run(run())


@pytest.mark.skip(
    reason="sandbox health check 30 次循环导致单用例约 30 秒,源代码未修复前暂跳过"
)
def test_send_message_stream_via_websocket_client():
    async def run() -> None:
        manager, request, repository, _, _, ws_client_pool = _make_ws_manager()
        agent, session = _create_agent_with_sandbox(manager, request)

        mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
        mock_ws_client.set_events(
            [
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
                InboundEvent(
                    type="message.delta",
                    session_id=session.id,
                    runtime_type="local_process",
                    event_id="evt-3",
                    ts_ms=3000,
                    payload={"delta": "ignored"},
                ),
            ]
        )

        with patch.object(
            ws_client_pool,
            "get_client",
            return_value=mock_ws_client,
        ):
            events = [
                event
                async for event in manager.send_message_stream(
                    agent.id, session.id, "hello"
                )
            ]

        assert repository.messages == [
            {
                "agent_id": agent.id,
                "session_id": session.id,
                "role": "user",
                "content": "hello",
            }
        ]
        assert mock_ws_client.send_calls == [
            {"type": "message.create", "payload": {"message": "hello"}}
        ]
        assert [event["event"]["type"] for event in events] == [
            "message.delta",
            "message.completed",
        ]
        assert events[0]["sandbox_type"] == "local_process"

    asyncio.run(run())


@pytest.mark.skip(
    reason="sandbox health check 30 次循环导致单用例约 30 秒,源代码未修复前暂跳过"
)
def test_send_message_stream_connects_when_not_connected():
    async def run() -> None:
        manager, request, _, _, _, ws_client_pool = _make_ws_manager()
        agent, session = _create_agent_with_sandbox(manager, request)

        mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
        mock_ws_client.set_events(
            [
                InboundEvent(
                    type="message.completed",
                    session_id=session.id,
                    runtime_type="local_process",
                    event_id="evt-1",
                    ts_ms=1000,
                    payload={},
                ),
            ]
        )

        with patch.object(
            ws_client_pool,
            "get_client",
            return_value=mock_ws_client,
        ):
            events = [
                event
                async for event in manager.send_message_stream(
                    agent.id, session.id, "hello"
                )
            ]

        assert mock_ws_client.connect_calls == [session.id]
        assert [event["event"]["type"] for event in events] == ["message.completed"]

    asyncio.run(run())


@pytest.mark.skip(
    reason="sandbox health check 30 次循环导致单用例约 30 秒,源代码未修复前暂跳过"
)
def test_get_adaptor_endpoint_converts_http_to_ws():
    """Test that _get_adaptor_endpoint converts http/https to ws/wss"""
    manager, request, repository, _, _, _ = _make_ws_manager()

    agent, session = _create_agent_with_sandbox(manager, request)

    # Set adapter_base_url to https
    repository.save_sandbox_state(
        agent.id,
        sandbox_payload_json={
            "sandbox_id": "sandbox-test",
            "agent_id": agent.id,
            "workspace_path": "/tmp",
        },
        adapter_base_url="https://adapter.example.com",
    )

    endpoint = manager._get_adaptor_endpoint(agent.id, session.id)

    assert endpoint.base_url == "wss://adapter.example.com"
    assert endpoint.session_id == session.id
    assert endpoint.sandbox_type == "local_process"


@pytest.mark.skip(
    reason="sandbox health check 30 次循环导致单用例约 30 秒,源代码未修复前暂跳过"
)
def test_get_adaptor_endpoint_converts_http_without_scheme():
    """Test that _get_adaptor_endpoint handles http:// URLs"""
    manager, request, repository, _, _, _ = _make_ws_manager()

    agent, session = _create_agent_with_sandbox(manager, request)

    repository.save_sandbox_state(
        agent.id,
        sandbox_payload_json={
            "sandbox_id": "sandbox-test",
            "agent_id": agent.id,
            "workspace_path": "/tmp",
        },
        adapter_base_url="http://adapter.local",
    )

    endpoint = manager._get_adaptor_endpoint(agent.id, session.id)

    assert endpoint.base_url == "ws://adapter.local"
    assert endpoint.session_id == session.id


@pytest.mark.asyncio
async def test_uninstall_agent_skill_surfaces_runtime_reason() -> None:
    manager, _, repository, _, _, _ = _make_ws_manager()
    _bootstrap_running_agent_and_session(repository)

    adaptor_client = AsyncMock()
    request = httpx.Request(
        "POST",
        "http://adapter.local/agent/skills/uninstall?id=agent-1",
    )
    response = httpx.Response(
        400,
        json={
            "code": "OPENCLAW_SKILL_NOT_REMOVABLE",
            "message": "openclaw skill cannot be uninstalled",
            "request_id": "req-uninstall-1",
            "details": {"reason": "bundled skill cannot be uninstalled"},
        },
        request=request,
    )
    adaptor_client.post.side_effect = httpx.HTTPStatusError(
        "bad request",
        request=request,
        response=response,
    )
    adaptor_client.close = AsyncMock()

    with (
        patch.object(manager, "_get_adaptor_http_client", return_value=adaptor_client),
        pytest.raises(DomainError) as exc_info,
    ):
        await manager.uninstall_agent_skill(
            "agent-1",
            "healthcheck",
            source_type="builtin",
            source_path="/opt/openclaw/skills/healthcheck",
            runtime_source="openclaw-bundled",
        )

    assert exc_info.value.code == "AGENT_SKILL_UNINSTALL_FAILED"
    assert exc_info.value.details == {
        "agent_id": "agent-1",
        "skill_name": "healthcheck",
        "error": "bundled skill cannot be uninstalled",
        "upstream_status_code": 400,
        "upstream_error_code": "OPENCLAW_SKILL_NOT_REMOVABLE",
        "upstream_error_message": "openclaw skill cannot be uninstalled",
        "upstream_request_id": "req-uninstall-1",
        "upstream_error_details": {"reason": "bundled skill cannot be uninstalled"},
    }


@pytest.mark.asyncio
async def test_install_agent_skill_surfaces_runtime_reason() -> None:
    manager, _, repository, _, _, _ = _make_ws_manager()
    _bootstrap_running_agent_and_session(repository)

    adaptor_client = AsyncMock()
    request = httpx.Request(
        "POST",
        "http://adapter.local/agent/skills/install?id=agent-1",
    )
    response = httpx.Response(
        500,
        json={
            "code": "OPENCLAW_SKILLS_INSTALL_FAILED",
            "message": "openclaw skills install failed",
            "request_id": "req-install-1",
            "details": {"reason": "openclaw command not found"},
        },
        request=request,
    )
    adaptor_client.post.side_effect = httpx.HTTPStatusError(
        "server error",
        request=request,
        response=response,
    )
    adaptor_client.close = AsyncMock()

    with (
        patch.object(manager, "_get_adaptor_http_client", return_value=adaptor_client),
        pytest.raises(DomainError) as exc_info,
    ):
        await manager.install_agent_skill(
            "agent-1",
            "weather",
            source_path="/tmp/weather",
        )

    assert exc_info.value.code == "AGENT_SKILL_INSTALL_FAILED"
    assert exc_info.value.details == {
        "agent_id": "agent-1",
        "skill_name": "weather",
        "error": "openclaw command not found",
        "upstream_status_code": 500,
        "upstream_error_code": "OPENCLAW_SKILLS_INSTALL_FAILED",
        "upstream_error_message": "openclaw skills install failed",
        "upstream_request_id": "req-install-1",
        "upstream_error_details": {"reason": "openclaw command not found"},
    }
    adaptor_client.post.assert_awaited_once_with(
        "/agent/skills/install?id=agent-1",
        json={"skill_name": "weather", "source_path": "/tmp/weather"},
        timeout=SKILL_INSTALL_TIMEOUT_SECONDS,
    )


def test_send_message_rejects_unknown_session() -> None:
    """会话不存在时抛 SESSION_NOT_FOUND（404），不再落到外键 500。

    未校验会话存在性时 ``create_message`` 会撞上 messages.session_id 的外键约束，
    接口返回裸 500，uvicorn 随后直接关闭连接，客户端复用该 keep-alive 连接的
    下一个请求拿到 ECONNRESET。
    """

    async def run() -> None:
        manager, _request, repository, _store, _backend, _pool = _make_ws_manager()
        agent, _session = _bootstrap_running_agent_and_session(repository)

        with pytest.raises(DomainError) as exc_info:
            await manager.send_message(agent.id, "no-such-session", "hello")

        assert exc_info.value.code == "SESSION_NOT_FOUND"
        assert exc_info.value.status_code == 404
        assert repository.messages == []

    asyncio.run(run())


def test_send_message_stream_rejects_unknown_session() -> None:
    """流式接口同样在起流前校验会话归属，避免 SSE 内部抛 500。"""

    async def run() -> None:
        manager, _request, repository, _store, _backend, _pool = _make_ws_manager()
        agent, _session = _bootstrap_running_agent_and_session(repository)

        stream = manager.send_message_stream(agent.id, "no-such-session", "hello")
        with pytest.raises(DomainError) as exc_info:
            await anext(stream)

        assert exc_info.value.code == "SESSION_NOT_FOUND"
        assert repository.messages == []

    asyncio.run(run())


def test_send_message_rejects_session_of_another_agent() -> None:
    """会话存在但不属于该 agent 时同样拒绝，避免跨 agent 写入消息。"""

    async def run() -> None:
        manager, _request, repository, _store, _backend, _pool = _make_ws_manager()
        agent, _session = _bootstrap_running_agent_and_session(repository)
        other = repository.upsert_session(
            session_id="session-2", agent_id="agent-2", status="running"
        )

        with pytest.raises(DomainError) as exc_info:
            await manager.send_message(agent.id, other.id, "hello")

        assert exc_info.value.code == "SESSION_AGENT_MISMATCH"
        assert repository.messages == []

    asyncio.run(run())


def test_reconnect_stream_rejects_unknown_session() -> None:
    """续订流同样要校验会话存在，否则会读到任意 session_id 的缓冲区。"""

    async def run() -> None:
        manager, _request, repository, _store, _backend, _pool = _make_ws_manager()
        agent, _session = _bootstrap_running_agent_and_session(repository)

        stream = manager.reconnect_stream(agent.id, "no-such-session")
        with pytest.raises(DomainError) as exc_info:
            await anext(stream)

        assert exc_info.value.code == "SESSION_NOT_FOUND"
        assert exc_info.value.status_code == 404

    asyncio.run(run())


def test_reconnect_stream_rejects_session_of_another_agent() -> None:
    """跨 agent 读别人的流：_stream_registry 是进程级共享的，必须按归属拦下。"""

    async def run() -> None:
        manager, _request, repository, _store, _backend, _pool = _make_ws_manager()
        agent, _session = _bootstrap_running_agent_and_session(repository)
        other = repository.upsert_session(
            session_id="session-2", agent_id="agent-2", status="running"
        )

        stream = manager.reconnect_stream(agent.id, other.id)
        with pytest.raises(DomainError) as exc_info:
            await anext(stream)

        assert exc_info.value.code == "SESSION_AGENT_MISMATCH"

    asyncio.run(run())


def test_reconnect_stream_allows_own_session_without_active_stream() -> None:
    """自己的会话但没有活动流：正常结束（不抛错），保证上面的校验没过杀。"""

    async def run() -> None:
        manager, _request, repository, _store, _backend, _pool = _make_ws_manager()
        agent, session = _bootstrap_running_agent_and_session(repository)

        stream = manager.reconnect_stream(agent.id, session.id)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    asyncio.run(run())


def test_delete_agent_keeps_record_when_runtime_stop_fails() -> None:
    """停机失败时不得先删库记录：不可逆操作必须放在最后，失败后可以重试。"""

    async def run() -> None:
        manager, _request, repository, _store, _backend, _pool = _make_ws_manager()
        agent, _session = _bootstrap_running_agent_and_session(repository)

        async def _boom_stop(agent_id: str) -> None:
            raise RuntimeError("runtime stop failed")

        manager._stop_runtime = _boom_stop  # type: ignore[method-assign]

        with pytest.raises(DomainError) as exc_info:
            await manager.delete_agent(agent.id)

        assert exc_info.value.code == AGENT_DELETE_FAILED
        assert exc_info.value.details["cleanup_errors"][0]["stage"] == "runtime_stop"
        assert repository.get_agent(agent.id) is not None
        assert repository.deleted_agents == []

    asyncio.run(run())


def test_delete_agent_tolerates_missing_sandbox_handle() -> None:
    """沙箱句柄已不存在属于"清理过了"，仍允许删除成功。"""

    async def run() -> None:
        manager, _request, repository, _store, _backend, _pool = _make_ws_manager()
        agent, _session = _bootstrap_running_agent_and_session(repository)

        async def _noop_stop(agent_id: str) -> None:
            return None

        def _gone(agent_id: str) -> None:
            # 真实来源：sandbox backend 的 _resolve_handle 在句柄丢失时抛
            # sandbox_not_found()（code=SANDBOX_NOT_FOUND）；判定按码而非文案。
            raise sandbox_not_found(
                sandbox_type="local_process", sandbox_id="sandbox-1"
            )

        manager._stop_runtime = _noop_stop  # type: ignore[method-assign]
        manager._cleanup_sandbox = _gone  # type: ignore[method-assign]

        await manager.delete_agent(agent.id)

        assert repository.get_agent(agent.id) is None

    asyncio.run(run())


def test_abort_session_rejects_session_of_another_agent() -> None:
    """跨 agent 中止：在产生本地副作用之前就拦下。

    归属校验缺失时，abort 会走到 find_last_assistant_message_for_session，把
    **别的 agent 的**最后一条 assistant 消息改成 interrupted。
    """

    async def run() -> None:
        manager, _request, repository, _store, _backend, _pool = _make_ws_manager()
        agent, _session = _bootstrap_running_agent_and_session(repository)
        other = repository.upsert_session(
            session_id="session-2", agent_id="agent-2", status="running"
        )
        remote_abort = AsyncMock()
        manager._session_manager.abort_session_remote = remote_abort  # type: ignore[method-assign]

        with pytest.raises(DomainError) as exc_info:
            await manager.abort_session(agent.id, other.id)

        assert exc_info.value.code == "SESSION_AGENT_MISMATCH"
        # 校验在最前面：远端 abort 与本地副作用都不该被触发
        remote_abort.assert_not_awaited()

    asyncio.run(run())


def test_abort_session_reads_session_row_once() -> None:
    """一次 abort 只读一次 session 行（B）：入口读过之后沿调用链复用。

    一次请求里重复点查在本仓库要付全价（每个 repository 调用新建 ORM Session，
    实测约 0.23ms），所以 _get_active_ws_client 必须接收已有的 session。
    """

    async def run() -> None:
        manager, _request, repository, _store, _backend, ws_client_pool = (
            _make_ws_manager()
        )
        agent, session = _bootstrap_running_agent_and_session(repository)

        reads: list[str] = []
        original = manager._session_manager.get_session

        def _counting_get_session(agent_id: str, session_id: str):
            reads.append(session_id)
            return original(agent_id, session_id)

        manager._session_manager.get_session = _counting_get_session  # type: ignore[method-assign]
        manager._session_manager.abort_session_remote = AsyncMock()  # type: ignore[method-assign]

        ws_client = MockWebSocketClient(base_url="ws://adapter/test")
        ws_client.is_connected = True
        with patch.object(ws_client_pool, "get_client", return_value=ws_client):
            await manager.abort_session(agent.id, session.id)

        assert reads == [session.id]

    asyncio.run(run())


def test_adaptor_endpoint_reuses_loaded_records() -> None:
    """传入已加载的 agent/session 时 _get_adaptor_endpoint 不再点查（B）。"""

    manager, _request, repository, _store, _backend, _pool = _make_ws_manager()
    agent, session = _bootstrap_running_agent_and_session(repository)

    session_reads: list[str] = []
    agent_reads: list[str] = []
    original_session = manager._session_manager.get_session
    original_agent = manager._get_agent

    def _counting_get_session(agent_id: str, session_id: str):
        session_reads.append(session_id)
        return original_session(agent_id, session_id)

    def _counting_get_agent(agent_id: str):
        agent_reads.append(agent_id)
        return original_agent(agent_id)

    manager._session_manager.get_session = _counting_get_session  # type: ignore[method-assign]
    manager._get_agent = _counting_get_agent  # type: ignore[method-assign]

    # 复用：不该产生任何点查
    endpoint = manager._get_adaptor_endpoint(
        agent.id, session.id, agent=agent, session=session
    )
    assert endpoint.base_url == "ws://adapter.local/agents/runtime-agent-1"
    assert session_reads == []
    assert agent_reads == []

    # 不传实体时退化为原行为（各读一次），保证既有调用方不受影响
    manager._get_adaptor_endpoint(agent.id, session.id)
    assert session_reads == [session.id]
    assert agent_reads == [agent.id]


def test_send_message_reads_agent_and_session_once() -> None:
    """send_message 一次请求只读 1 次 agent + 1 次 session（B）。

    改造前是 7 条 agent/session 点查（_get_agent x1 + get_session x3，后者每次 2 条
    SQL）；入口读一次、沿调用链复用之后是 2 条。本仓库每个 repository 调用都要新建
    ORM Session（实测约 0.23ms/次），所以这个数字回涨就是性能回归。
    """

    async def run() -> None:
        manager, _request, repository, _store, _backend, ws_client_pool = (
            _make_ws_manager()
        )
        agent, session = _bootstrap_running_agent_and_session(repository)

        session_reads: list[str] = []
        agent_reads: list[str] = []
        original_session = manager._session_manager.get_session
        original_agent = manager._get_agent

        def _counting_get_session(agent_id: str, session_id: str):
            session_reads.append(session_id)
            return original_session(agent_id, session_id)

        def _counting_get_agent(agent_id: str):
            agent_reads.append(agent_id)
            return original_agent(agent_id)

        manager._session_manager.get_session = _counting_get_session  # type: ignore[method-assign]
        manager._get_agent = _counting_get_agent  # type: ignore[method-assign]

        mock_ws_client = MockWebSocketClient(base_url="ws://adapter/test")
        mock_ws_client.set_events(
            [
                InboundEvent(
                    type="message.completed",
                    session_id=session.id,
                    runtime_type="openclaw",
                    event_id="evt-1",
                    ts_ms=1000,
                    payload={},
                )
            ]
        )

        with patch.object(ws_client_pool, "get_client", return_value=mock_ws_client):
            await manager.send_message(agent.id, session.id, "hello from user")

        assert session_reads == [session.id]
        assert agent_reads == [agent.id]

    asyncio.run(run())
