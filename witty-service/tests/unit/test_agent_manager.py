from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from witty_service.application.agent_manager import AgentCreateRequest, AgentManager
from witty_service.application.session_manager import SessionManager
from witty_service.domain.enums import AgentStatus
from witty_service.domain.errors import DomainError
from witty_service.persistence.db import create_session_factory, create_sqlite_engine, init_db
from witty_service.persistence.repositories import AgentRecord, SessionRecord, SqliteRepository
from witty_service.runtime.base import AdapterEndpoint, RUNTIME_NOT_FOUND, RuntimeHandle
from witty_service.storage.workspace_store import WorkspaceStore


@dataclass(slots=True)
class FakeRuntimeState:
    agent_id: str
    runtime_payload_json: dict[str, object]
    adapter_base_url: str
    adapter_ready: bool = True
    last_error: str | None = None

    @property
    def handle(self) -> RuntimeHandle:
        return RuntimeHandle(
            runtime_id=self.runtime_payload_json["runtime_id"],
            agent_id=self.runtime_payload_json["agent_id"],
            workspace_path=self.runtime_payload_json["workspace_path"],
            metadata=self.runtime_payload_json.get("metadata", {}),
        )


class FakeRepository:
    def __init__(self) -> None:
        self.agent_counter = 0
        self.session_counter = 0
        self.agents: dict[str, AgentRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.runtime_states: dict[str, FakeRuntimeState] = {}
        self.messages: list[dict[str, str]] = []
        self.deleted_agents: list[str] = []
        self.fail_on_save_runtime_state = False
        self.fail_on_update_status_for: dict[str, Exception] = {}

    def create_agent(
        self,
        *,
        name: str,
        runtime_type: str,
        adapter_type: str,
        workspace_path: str,
        idle_timeout_seconds: int,
        status: AgentStatus = AgentStatus.creating,
        sandbox_id: str | None = None,
        has_scheduled_tasks: bool = False,
        last_active_at: datetime | None = None,
    ) -> AgentRecord:
        self.agent_counter += 1
        now = datetime.now(UTC)
        agent = AgentRecord(
            id=f"agent-{self.agent_counter}",
            name=name,
            runtime_type=runtime_type,
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

    def create_agent_with_id(
        self,
        *,
        agent_id: str,
        name: str,
        runtime_type: str,
        adapter_type: str,
        workspace_path: str,
        idle_timeout_seconds: int,
        status: AgentStatus = AgentStatus.creating,
        sandbox_id: str | None = None,
        has_scheduled_tasks: bool = False,
        last_active_at: datetime | None = None,
    ) -> AgentRecord:
        now = datetime.now(UTC)
        agent = AgentRecord(
            id=agent_id,
            name=name,
            runtime_type=runtime_type,
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

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        return self.agents.get(agent_id)

    def update_agent_status(
        self,
        agent_id: str,
        status: AgentStatus,
        updated_at: datetime | None = None,
    ) -> AgentRecord:
        error = self.fail_on_update_status_for.get(agent_id)
        if error is not None:
            raise error
        current = self.agents[agent_id]
        updated = AgentRecord(
            id=current.id,
            name=current.name,
            runtime_type=current.runtime_type,
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

    def create_session(self, agent_id: str) -> SessionRecord:
        self.session_counter += 1
        now = datetime.now(UTC)
        session = SessionRecord(
            id=f"session-{self.session_counter}",
            agent_id=agent_id,
            status="active",
            created_at=now,
            updated_at=now,
        )
        self.sessions[session.id] = session
        return session

    def get_session(self, session_id: str) -> SessionRecord | None:
        return self.sessions.get(session_id)

    def save_runtime_state(
        self,
        agent_id: str,
        *,
        runtime_payload_json: dict[str, object],
        adapter_base_url: str,
        adapter_ready: bool = True,
        last_error: str | None = None,
    ) -> FakeRuntimeState:
        if self.fail_on_save_runtime_state:
            raise RuntimeError("save_runtime_state failed")
        state = FakeRuntimeState(
            agent_id=agent_id,
            runtime_payload_json=runtime_payload_json,
            adapter_base_url=adapter_base_url,
            adapter_ready=adapter_ready,
            last_error=last_error,
        )
        self.runtime_states[agent_id] = state
        return state

    def get_runtime_state(self, agent_id: str) -> FakeRuntimeState | None:
        return self.runtime_states.get(agent_id)

    def create_message(
        self,
        *,
        agent_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata_json: dict[str, object] | None = None,
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
        self.runtime_states.pop(agent_id, None)

class FakeWorkspaceStore:
    def __init__(self) -> None:
        self.init_calls: list[str] = []
        self.cleanup_calls: list[str] = []
        self.fail_on_cleanup = False

    def init_workspace(self, agent_id: str) -> Path:
        self.init_calls.append(agent_id)
        return Path("/tmp") / agent_id / "workspace"

    def cleanup_workspace(self, agent_id: str) -> None:
        if self.fail_on_cleanup:
            raise RuntimeError("workspace cleanup failed")
        self.cleanup_calls.append(agent_id)


class FakeRuntimeBackend:
    runtime_type = "local_process"

    def __init__(self, event_log: list[str] | None = None) -> None:
        self.start_calls: list[dict[str, str]] = []
        self.stop_calls: list[str] = []
        self.cleanup_calls: list[str] = []
        self.event_log = event_log if event_log is not None else []
        self.fail_on_cleanup = False

    def start(self, *, agent_id: str, workspace_path: str, **_: object) -> RuntimeHandle:
        self.event_log.append(f"runtime.start:{agent_id}")
        self.start_calls.append(
            {
                "agent_id": agent_id,
                "workspace_path": workspace_path,
            }
        )
        return RuntimeHandle(
            runtime_id=f"runtime-{agent_id}",
            agent_id=agent_id,
            workspace_path=workspace_path,
            metadata={},
        )

    def stop(self, handle: RuntimeHandle | str, **_: object) -> None:
        runtime_id = handle.runtime_id if isinstance(handle, RuntimeHandle) else handle
        self.event_log.append(f"runtime.stop:{runtime_id}")
        self.stop_calls.append(runtime_id)

    def endpoint(self, handle: RuntimeHandle | str, **_: object) -> AdapterEndpoint:
        runtime_id = handle.runtime_id if isinstance(handle, RuntimeHandle) else handle
        return AdapterEndpoint(base_url=f"http://adapter/{runtime_id}", health_url=None)

    def cleanup(self, handle: RuntimeHandle | str, **_: object) -> None:
        runtime_id = handle.runtime_id if isinstance(handle, RuntimeHandle) else handle
        self.event_log.append(f"runtime.cleanup:{runtime_id}")
        self.cleanup_calls.append(runtime_id)
        if self.fail_on_cleanup:
            raise RuntimeError("runtime cleanup failed")


class FakeAdapterClient:
    def __init__(
        self,
        base_url: str,
        event_log: list[str] | None = None,
        fail_on_start: bool = False,
        fail_on_stop: bool = False,
    ) -> None:
        self.base_url = base_url
        self.start_calls = 0
        self.stop_calls = 0
        self.stream_calls: list[tuple[str, str]] = []
        self.events = [
            {"type": "delta", "delta": "hello"},
            {"type": "done"},
        ]
        self.event_log = event_log if event_log is not None else []
        self.fail_on_start = fail_on_start
        self.fail_on_stop = fail_on_stop

    def start(self, *, reload: bool = False) -> dict[str, object]:
        self.event_log.append(f"adapter.start:{self.base_url}")
        self.start_calls += 1
        if self.fail_on_start:
            raise RuntimeError("adapter failed to start")
        return {
            "status": "running",
            "runtime_type": "openclaw",
            "config": {},
            "already_running": False,
            "reload": reload,
        }

    def stop(self) -> dict[str, object]:
        self.event_log.append(f"adapter.stop:{self.base_url}")
        self.stop_calls += 1
        if self.fail_on_stop:
            raise RuntimeError("adapter stop failed")
        return {
            "status": "stopped",
            "runtime_type": "openclaw",
            "config": {},
        }

    def create_session(self) -> dict[str, object]:
        return {"id": "adapter-session", "context_initialized": True}

    def send_message_stream(self, session_id: str, message: str):
        self.stream_calls.append((session_id, message))
        yield from self.events


class FakeAdapterClientFactory:
    def __init__(
        self,
        event_log: list[str] | None = None,
        fail_on_start_for: set[str] | None = None,
        fail_on_any_start: bool = False,
    ) -> None:
        self.clients: dict[str, FakeAdapterClient] = {}
        self.event_log = event_log if event_log is not None else []
        self.fail_on_start_for = fail_on_start_for if fail_on_start_for is not None else set()
        self.fail_on_any_start = fail_on_any_start

    def __call__(self, base_url: str) -> FakeAdapterClient:
        client = self.clients.get(base_url)
        if client is None:
            client = FakeAdapterClient(
                base_url,
                event_log=self.event_log,
                fail_on_start=self.fail_on_any_start or base_url in self.fail_on_start_for,
            )
            self.clients[base_url] = client
        return client


def _make_manager(
    *,
    fail_on_adapter_start_for: set[str] | None = None,
    fail_on_any_adapter_start: bool = False,
):
    repository = FakeRepository()
    workspace_store = FakeWorkspaceStore()
    event_log: list[str] = []
    runtime_backend = FakeRuntimeBackend(event_log=event_log)
    adapter_factory = FakeAdapterClientFactory(
        event_log=event_log,
        fail_on_start_for=fail_on_adapter_start_for,
        fail_on_any_start=fail_on_any_adapter_start,
    )
    session_manager = SessionManager(repository)
    manager = AgentManager(
        repository=repository,
        session_manager=session_manager,
        workspace_store=workspace_store,
        runtime_backend=runtime_backend,
        adapter_client_factory=adapter_factory,
    )
    request = AgentCreateRequest(
        name="demo",
        runtime_type="local_process",
        adapter_type="http",
        idle_timeout_seconds=300,
    )
    return (
        manager,
        request,
        repository,
        workspace_store,
        runtime_backend,
        adapter_factory,
        event_log,
    )


def test_create_agent_runs_minimal_orchestration_chain():
    manager, request, repository, workspace_store, runtime_backend, adapter_factory, _ = (
        _make_manager()
    )

    result = manager.create_agent(request)

    assert result.agent.status is AgentStatus.running
    assert result.default_session.agent_id == result.agent.id
    assert workspace_store.init_calls == [result.agent.id]
    assert runtime_backend.start_calls == [
        {
            "agent_id": result.agent.id,
            "workspace_path": result.agent.workspace_path,
        }
    ]
    runtime_state = repository.get_runtime_state(result.agent.id)
    assert runtime_state is not None
    assert runtime_state.handle.runtime_id == f"runtime-{result.agent.id}"
    adapter_client = adapter_factory.clients[runtime_state.adapter_base_url]
    assert adapter_client.start_calls == 1
    assert repository.get_session(result.default_session.id) == result.default_session


def test_pause_and_resume_follow_valid_status_transitions():
    manager, request, repository, _, runtime_backend, adapter_factory, event_log = _make_manager()
    created = manager.create_agent(request)
    runtime_state = repository.get_runtime_state(created.agent.id)
    assert runtime_state is not None
    adapter_client = adapter_factory.clients[runtime_state.adapter_base_url]

    paused = manager.pause_agent(created.agent.id)
    resumed = manager.resume_agent(created.agent.id)

    assert paused.status is AgentStatus.paused
    assert resumed.status is AgentStatus.running
    assert adapter_client.stop_calls == 1
    assert adapter_client.start_calls == 2
    assert runtime_backend.stop_calls == []
    assert runtime_backend.cleanup_calls == [f"runtime-{created.agent.id}"]
    assert event_log[-2:] == [
        f"runtime.start:{created.agent.id}",
        f"adapter.start:http://adapter/runtime-{created.agent.id}",
    ]


def test_send_message_auto_resumes_when_agent_is_paused():
    manager, request, repository, _, runtime_backend, adapter_factory, event_log = _make_manager()
    created = manager.create_agent(request)
    manager.pause_agent(created.agent.id)
    runtime_state = repository.get_runtime_state(created.agent.id)
    assert runtime_state is not None
    adapter_client = adapter_factory.clients[runtime_state.adapter_base_url]

    events = manager.send_message(
        created.agent.id,
        created.default_session.id,
        "hello from user",
    )

    assert [event["type"] for event in events] == ["delta", "done"]
    assert adapter_client.start_calls == 2
    assert adapter_client.stream_calls == [
        (created.default_session.id, "hello from user")
    ]
    assert event_log[-2:] == [
        f"runtime.start:{created.agent.id}",
        f"adapter.start:http://adapter/runtime-{created.agent.id}",
    ]
    assert runtime_backend.start_calls[-1]["agent_id"] == created.agent.id
    assert repository.messages == [
        {
            "agent_id": created.agent.id,
            "session_id": created.default_session.id,
            "role": "user",
            "content": "hello from user",
        }
    ]
    assert repository.get_agent(created.agent.id).status is AgentStatus.running


def test_delete_agent_rejects_running_agent_without_runtime_state():
    manager, request, repository, _, _, _, _ = _make_manager()
    created = manager.create_agent(request)
    repository.runtime_states.clear()

    with pytest.raises(DomainError) as exc_info:
        manager.delete_agent(created.agent.id)

    assert exc_info.value.code == RUNTIME_NOT_FOUND


def test_delete_agent_best_effort_continues_when_stop_and_runtime_cleanup_fail():
    manager, request, repository, workspace_store, runtime_backend, adapter_factory, _ = (
        _make_manager()
    )
    created = manager.create_agent(request)
    runtime_state = repository.get_runtime_state(created.agent.id)
    assert runtime_state is not None
    adapter_client = adapter_factory.clients[runtime_state.adapter_base_url]
    adapter_client.fail_on_stop = True
    runtime_backend.fail_on_cleanup = True

    with pytest.raises(DomainError) as exc_info:
        manager.delete_agent(created.agent.id)

    assert exc_info.value.code == "AGENT_DELETE_FAILED"
    assert exc_info.value.details["cleanup_errors"] == [
        {"stage": "adapter_stop", "error": "adapter stop failed"},
        {"stage": "runtime_cleanup", "error": "runtime cleanup failed"},
    ]
    assert workspace_store.cleanup_calls == [created.agent.id]
    assert repository.deleted_agents == [created.agent.id]
    assert repository.get_agent(created.agent.id) is None
    assert repository.get_runtime_state(created.agent.id) is None
    assert runtime_backend.cleanup_calls == [f"runtime-{created.agent.id}"]
    assert adapter_client.stop_calls == 1


def test_delete_agent_attempts_runtime_cleanup_when_runtime_state_is_not_adapter_ready():
    manager, request, repository, _, runtime_backend, _, _ = _make_manager()
    created = manager.create_agent(request)
    manager.pause_agent(created.agent.id)
    runtime_state = repository.get_runtime_state(created.agent.id)
    assert runtime_state is not None
    assert runtime_state.adapter_ready is False

    manager.delete_agent(created.agent.id)

    assert runtime_backend.cleanup_calls == [
        f"runtime-{created.agent.id}",
        f"runtime-{created.agent.id}",
    ]
    assert repository.get_agent(created.agent.id) is None
    assert repository.get_runtime_state(created.agent.id) is None


def test_delete_agent_continues_after_resume_cleanup_failure():
    manager, request, repository, workspace_store, runtime_backend, adapter_factory, _ = (
        _make_manager()
    )
    created = manager.create_agent(request)
    manager.pause_agent(created.agent.id)
    runtime_state = repository.get_runtime_state(created.agent.id)
    assert runtime_state is not None
    adapter_client = adapter_factory.clients[runtime_state.adapter_base_url]
    adapter_client.fail_on_start = True
    runtime_backend.fail_on_cleanup = True

    with pytest.raises(DomainError):
        manager.resume_agent(created.agent.id)

    with pytest.raises(DomainError) as exc_info:
        manager.delete_agent(created.agent.id)

    assert exc_info.value.code == "AGENT_DELETE_FAILED"
    assert exc_info.value.details["cleanup_errors"] == [
        {"stage": "runtime_cleanup", "error": "runtime cleanup failed"},
    ]
    assert workspace_store.cleanup_calls == [created.agent.id]
    assert repository.deleted_agents == [created.agent.id]
    assert repository.get_agent(created.agent.id) is None
    assert repository.get_runtime_state(created.agent.id) is None
    assert runtime_backend.cleanup_calls == [
        f"runtime-{created.agent.id}",
        f"runtime-{created.agent.id}",
        f"runtime-{created.agent.id}",
    ]


def test_create_agent_runs_with_sqlite_repository(tmp_path):
    repository = _build_repository(tmp_path / "repository.sqlite3")
    workspace_store = WorkspaceStore(base_dir=tmp_path / "workspaces")
    runtime_backend = FakeRuntimeBackend()
    adapter_factory = FakeAdapterClientFactory()
    manager = AgentManager(
        repository=repository,
        session_manager=SessionManager(repository),
        workspace_store=workspace_store,
        runtime_backend=runtime_backend,
        adapter_client_factory=adapter_factory,
    )

    result = manager.create_agent(
        AgentCreateRequest(
            name="sqlite-agent",
            runtime_type="local_process",
            adapter_type="http",
            idle_timeout_seconds=300,
        )
    )

    stored_agent = repository.get_agent(result.agent.id)
    runtime_state = repository.get_runtime_state(result.agent.id)
    stored_session = repository.get_session(result.default_session.id)

    assert stored_agent is not None
    assert stored_agent.status is AgentStatus.running
    assert runtime_state is not None
    assert runtime_state.handle.runtime_id == f"runtime-{result.agent.id}"
    assert runtime_state.adapter_base_url == f"http://adapter/runtime-{result.agent.id}"
    assert stored_session is not None
    assert stored_session.agent_id == result.agent.id


def test_pause_agent_stops_and_cleans_up_runtime():
    manager, request, repository, _, runtime_backend, adapter_factory, _ = _make_manager()
    created = manager.create_agent(request)
    runtime_state = repository.get_runtime_state(created.agent.id)
    assert runtime_state is not None
    adapter_client = adapter_factory.clients[runtime_state.adapter_base_url]

    paused = manager.pause_agent(created.agent.id)

    assert paused.status is AgentStatus.paused
    assert adapter_client.stop_calls == 1
    assert runtime_backend.stop_calls == []
    assert runtime_backend.cleanup_calls == [f"runtime-{created.agent.id}"]


def test_resume_agent_cleans_up_runtime_when_adapter_start_fails():
    manager, request, repository, _, runtime_backend, adapter_factory, _ = _make_manager()
    created = manager.create_agent(request)
    manager.pause_agent(created.agent.id)
    runtime_state = repository.get_runtime_state(created.agent.id)
    assert runtime_state is not None
    adapter_client = adapter_factory.clients[runtime_state.adapter_base_url]
    adapter_client.fail_on_start = True

    with pytest.raises(DomainError) as exc_info:
        manager.resume_agent(created.agent.id)

    assert exc_info.value.code == "AGENT_RESUME_FAILED"
    assert exc_info.value.details["cause"] == "adapter failed to start"
    assert runtime_backend.stop_calls == []
    assert runtime_backend.cleanup_calls == [
        f"runtime-{created.agent.id}",
        f"runtime-{created.agent.id}",
    ]
    rolled_back_state = repository.get_runtime_state(created.agent.id)
    assert rolled_back_state is not None
    assert rolled_back_state.adapter_ready is False
    assert rolled_back_state.last_error == "adapter failed to start"
    assert repository.get_agent(created.agent.id).status is AgentStatus.paused


def test_resume_agent_keeps_runtime_state_when_cleanup_fails():
    manager, request, repository, _, runtime_backend, adapter_factory, _ = _make_manager()
    created = manager.create_agent(request)
    manager.pause_agent(created.agent.id)
    runtime_state = repository.get_runtime_state(created.agent.id)
    assert runtime_state is not None
    adapter_client = adapter_factory.clients[runtime_state.adapter_base_url]
    adapter_client.fail_on_start = True
    runtime_backend.fail_on_cleanup = True

    with pytest.raises(DomainError) as exc_info:
        manager.resume_agent(created.agent.id)

    assert exc_info.value.code == "AGENT_RESUME_FAILED"
    assert exc_info.value.details["cleanup_errors"] == [
        {
            "stage": "runtime_cleanup",
            "error": "runtime cleanup failed",
        }
    ]
    rolled_back_state = repository.get_runtime_state(created.agent.id)
    assert rolled_back_state is not None
    assert rolled_back_state.adapter_ready is True
    assert rolled_back_state.last_error is None
    assert repository.get_agent(created.agent.id).status is AgentStatus.paused


def test_create_agent_failure_includes_cleanup_errors():
    (
        manager,
        request,
        repository,
        workspace_store,
        runtime_backend,
        _,
        _,
    ) = _make_manager(
        fail_on_any_adapter_start=True
    )
    runtime_backend.fail_on_cleanup = True
    workspace_store.fail_on_cleanup = True

    with pytest.raises(DomainError) as exc_info:
        manager.create_agent(request)

    assert exc_info.value.code == "AGENT_CREATE_FAILED"
    assert exc_info.value.details["cause"] == "adapter failed to start"
    assert exc_info.value.details["cleanup_errors"] == [
        {"stage": "runtime_cleanup", "error": "runtime cleanup failed"},
        {"stage": "workspace_cleanup", "error": "workspace cleanup failed"},
    ]
    assert repository.agents == {}
    assert repository.runtime_states == {}
    assert len(runtime_backend.cleanup_calls) == 1
    assert workspace_store.cleanup_calls == []


def _build_repository(db_path: Path) -> SqliteRepository:
    engine = create_sqlite_engine(f"sqlite:///{db_path}")
    init_db(engine)
    return SqliteRepository(create_session_factory(engine))
