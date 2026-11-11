from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from witty_service.application.turn_service import TurnService
from witty_service.domain.enums import AgentKind, SandboxType, SessionOrigin, TurnStatus
from witty_service.domain.errors import DomainError
from witty_service.persistence.entities import Base
from witty_service.persistence.repository import (
    AgentRepository,
    CatalogRepository,
    SessionRepository,
    WorkspaceRepository,
)
from witty_service.persistence.repository.session import SelectionChangedError
from witty_service.runtime.contracts import (
    RuntimeEmptyPayload,
    RuntimeErrorPayload,
    RuntimeEventEnvelope,
    RuntimeEventType,
    RuntimeFilePart,
    RuntimeSession,
    RuntimeTextPart,
    RuntimeWorkspace,
    TurnAdmission,
    TurnPermit,
)
from witty_service.runtime.errors import RuntimeUnavailableError
from witty_service.runtime.events import (
    RuntimeResyncReason,
    RuntimeResyncRequired,
    RuntimeTurnEvent,
)
from witty_service.runtime.manager import RuntimePhase, WorkspaceRuntimeSpec


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.released: list[str] = []
        self.events: asyncio.Queue[RuntimeTurnEvent | None] = asyncio.Queue()
        self.fail_at: str | None = None
        self.runtime_session_id = "rt_1"

    def get_state(self, workspace_id: str):
        return SimpleNamespace(workspace_id=workspace_id, phase=RuntimePhase.HEALTHY)

    def _maybe_fail(self, name: str) -> None:
        if self.fail_at == name:
            raise RuntimeUnavailableError(workspace_id="ws_1", detail=f"{name} failed")

    async def ensure_started(self, spec: WorkspaceRuntimeSpec) -> None:
        self._maybe_fail("ensure_started")
        self.calls.append(("ensure_started", spec.workspace.workspace_id))

    async def acquire_turn_permit(
        self,
        spec: WorkspaceRuntimeSpec,
        *,
        turn_id: str,
        runtime_session_id: str | None = None,
    ) -> TurnPermit:
        self._maybe_fail("acquire_turn_permit")
        self.calls.append(("acquire_turn_permit", turn_id))
        return TurnPermit(
            permit_id=f"permit_{turn_id}",
            workspace_id=spec.workspace.workspace_id,
            turn_id=turn_id,
            published_projection_hash="hash",
        )

    async def release_turn_permit(self, permit: TurnPermit) -> None:
        self.released.append(permit.turn_id)

    async def ensure_runtime_session(
        self,
        *,
        workspace_id: str,
        session_id: str,
        runtime_session_id: str | None,
    ) -> RuntimeSession:
        self._maybe_fail("ensure_runtime_session")
        self.calls.append(("ensure_runtime_session", runtime_session_id))
        return RuntimeSession(
            workspace_id=workspace_id,
            session_id=session_id,
            runtime_session_id=self.runtime_session_id,
        )

    async def bind_turn_runtime_session(
        self, *, workspace_id: str, turn_id: str, runtime_session_id: str
    ) -> None:
        self.calls.append(("bind_turn_runtime_session", runtime_session_id))

    async def admit_turn(self, request) -> TurnAdmission:
        self._maybe_fail("admit_turn")
        self.calls.append(("admit_turn", request))
        return TurnAdmission(
            message_id=request.message_id,
            runtime_session_id=request.runtime_session_id,
            admitted_seq=1,
            delivery="immediate",
            admitted_at=1_000,
        )

    async def subscribe_events(self, workspace_id: str):
        while True:
            item = await self.events.get()
            if item is None:
                return
            yield item


@pytest.fixture()
def env():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    agents = AgentRepository(factory)
    catalog = CatalogRepository(factory)
    workspaces = WorkspaceRepository(factory)
    sessions = SessionRepository(factory)
    agent = agents.create("Primary", AgentKind.primary, 1_000)
    model = catalog.create_model(
        "Model", "provider", "model-1", None, None, None, 4096, 0.2, 1_000
    )
    workspace = workspaces.create(
        "Default", "/work", SandboxType.local, agent.id, None, 1, 1_000
    )
    runtime = FakeRuntime()
    service = TurnService(
        sessions=sessions,
        workspaces=workspaces,
        agents=agents,
        catalog=catalog,
        runtime=runtime,
        runtime_spec_factory=lambda item: WorkspaceRuntimeSpec(
            workspace=RuntimeWorkspace(
                workspace_id=item.id, runtime_directory=item.host_directory
            ),
            host_directory=item.host_directory,
            command=("opencode",),
        ),
    )
    yield SimpleNamespace(
        service=service,
        sessions=sessions,
        agents=agents,
        catalog=catalog,
        workspace_id=workspace.id,
        agent_id=agent.id,
        model_id=model.id,
        runtime=runtime,
    )
    engine.dispose()


@pytest.fixture()
def service(env):
    return (
        env.service,
        env.sessions,
        env.agents,
        env.catalog,
        env.workspace_id,
        env.agent_id,
        env.model_id,
    )


def _create_session(sessions, workspace_id, agent_id, model_id):
    return sessions.create(
        workspace_id, agent_id, model_id, SessionOrigin.web, None, None, None, 1_000
    )


def test_start_turn_allocates_sequence_and_snapshots(service) -> None:
    turns, sessions, _, _, workspace_id, agent_id, model_id = service
    session = _create_session(sessions, workspace_id, agent_id, model_id)

    first = turns.start_turn(session.id)
    assert first.turn_seq == 1
    assert first.agent_id == agent_id
    assert first.model_id == model_id
    assert first.agent_name_snapshot == "Primary"
    assert first.model_name_snapshot == "Model"

    sessions.finish_turn(first.id, TurnStatus.completed, None, None, 1_100)
    second = turns.start_turn(session.id)
    assert second.turn_seq == 2


def test_start_turn_uses_caller_turn_id(service) -> None:
    turns, sessions, *_, workspace_id, agent_id, model_id = service
    session = _create_session(sessions, workspace_id, agent_id, model_id)

    turn = turns.start_turn(session.id, turn_id="trn_fixed")

    assert turn.id == "trn_fixed"
    assert sessions.get_running_turn(session.id).id == "trn_fixed"


def test_start_turn_conflicts_with_running_turn(service) -> None:
    turns, sessions, _, _, workspace_id, agent_id, model_id = service
    session = _create_session(sessions, workspace_id, agent_id, model_id)
    running = turns.start_turn(session.id)

    with pytest.raises(DomainError) as caught:
        turns.start_turn(session.id)

    assert caught.value.code == "ACTIVE_TURN_CONFLICT"
    assert caught.value.status_code == 409
    assert caught.value.details["active_turn_id"] == running.id
    assert len(sessions.list_turns(session.id)) == 1


def test_start_turn_requires_session_model(service) -> None:
    turns, sessions, _, _, workspace_id, agent_id, _ = service
    session = _create_session(sessions, workspace_id, agent_id, None)

    with pytest.raises(DomainError) as caught:
        turns.start_turn(session.id)

    assert caught.value.code == "MODEL_REQUIRED"
    assert caught.value.status_code == 400


def test_start_turn_rejects_archived_agent_and_disabled_model(service) -> None:
    turns, sessions, agents, catalog, workspace_id, agent_id, model_id = service
    session = _create_session(sessions, workspace_id, agent_id, model_id)
    catalog.disable_model(model_id, 1_100)

    with pytest.raises(DomainError) as disabled:
        turns.start_turn(session.id)
    assert disabled.value.code == "MODEL_DISABLED"
    assert disabled.value.status_code == 409

    catalog.update_model(
        model_id,
        display_name="Model",
        max_tokens=4096,
        temperature=0.2,
        enabled=1,
        is_default=0,
        now=1_200,
    )
    expert = agents.create("Expert", AgentKind.expert, 1_200)
    sessions.update_current_settings(session.id, expert.id, None, 1_200)
    agents.archive(expert.id, 1_300)

    with pytest.raises(DomainError) as archived:
        turns.start_turn(session.id)
    assert archived.value.code == "AGENT_ARCHIVED"
    assert archived.value.status_code == 409


def test_start_turn_rejects_deleted_session(service) -> None:
    turns, sessions, _, _, workspace_id, agent_id, model_id = service
    session = _create_session(sessions, workspace_id, agent_id, model_id)
    sessions.mark_deleted(session.id, 1_100)

    with pytest.raises(DomainError) as caught:
        turns.start_turn(session.id)

    assert caught.value.code == "SESSION_ADMISSION_CLOSED"
    assert caught.value.status_code == 409


def test_deleted_session_without_model_reports_model_required(service) -> None:
    turns, sessions, _, _, workspace_id, agent_id, _ = service
    session = _create_session(sessions, workspace_id, agent_id, None)
    sessions.mark_deleted(session.id, 1_100)

    with pytest.raises(DomainError) as caught:
        turns.start_turn(session.id)

    assert caught.value.code == "MODEL_REQUIRED"
    assert caught.value.status_code == 400


def test_start_turn_uses_session_selection_inside_transaction(service) -> None:
    _, sessions, _, catalog, workspace_id, agent_id, model_id = service
    session = _create_session(sessions, workspace_id, agent_id, model_id)
    replacement = catalog.create_model(
        "Replacement", "provider", "model-2", None, None, None, 4096, 0.2, 1_100
    )
    sessions.update_current_settings(session.id, None, replacement.id, 1_200)

    with pytest.raises(SelectionChangedError):
        sessions.start_turn(session.id, agent_id, model_id, 1_300)

    turn = sessions.start_turn(session.id, None, None, 1_300)
    assert turn.model_id == replacement.id
    assert turn.model_name_snapshot == "Replacement"


def _completed_event(workspace_id: str, turn_id: str) -> RuntimeTurnEvent:
    return RuntimeTurnEvent(
        workspace_id=workspace_id,
        turn_id=turn_id,
        event=RuntimeEventEnvelope(
            workspace_id=workspace_id,
            runtime_session_id="rt_1",
            event_id="evt_1",
            type=RuntimeEventType.TURN_COMPLETED,
            durable_seq=1,
            occurred_at=1_100,
            payload=RuntimeEmptyPayload(),
        ),
    )


def _failed_event(workspace_id: str, turn_id: str) -> RuntimeTurnEvent:
    return RuntimeTurnEvent(
        workspace_id=workspace_id,
        turn_id=turn_id,
        event=RuntimeEventEnvelope(
            workspace_id=workspace_id,
            runtime_session_id="rt_1",
            event_id="evt_2",
            type=RuntimeEventType.TURN_FAILED,
            durable_seq=2,
            occurred_at=1_200,
            payload=RuntimeErrorPayload(code="MODEL_ERROR", message="model exploded"),
        ),
    )


@pytest.mark.asyncio
async def test_execute_turn_admits_and_keeps_permit_until_terminal(env) -> None:
    session = _create_session(
        env.sessions, env.workspace_id, env.agent_id, env.model_id
    )
    try:
        turn = await env.service.execute_turn(
            session.id, parts=(RuntimeTextPart(text="hello"),)
        )

        assert turn.status is TurnStatus.running
        assert [name for name, _ in env.runtime.calls] == [
            "ensure_started",
            "acquire_turn_permit",
            "ensure_runtime_session",
            "bind_turn_runtime_session",
            "admit_turn",
        ]
        request = env.runtime.calls[-1][1]
        assert request.message_id == f"msg_{turn.id}"
        assert env.agent_id in request.agent_internal_name
        assert request.model.model_id == "model-1"
        assert env.sessions.get(session.id).runtime_session_id == "rt_1"
        assert env.runtime.released == []

        await env.runtime.events.put(_completed_event(env.workspace_id, turn.id))
        updated = turn
        for _ in range(100):
            updated = env.sessions.get_turn(turn.id)
            if updated.status is TurnStatus.completed:
                break
            await asyncio.sleep(0.01)

        assert updated.status is TurnStatus.completed
        assert env.runtime.released == [turn.id]
    finally:
        await env.service.close()


@pytest.mark.asyncio
async def test_execute_turn_fails_without_insert_when_permit_unavailable(env) -> None:
    session = _create_session(
        env.sessions, env.workspace_id, env.agent_id, env.model_id
    )
    env.runtime.fail_at = "acquire_turn_permit"
    try:
        with pytest.raises(DomainError) as caught:
            await env.service.execute_turn(
                session.id, parts=(RuntimeTextPart(text="hello"),)
            )

        assert caught.value.code == "RUNTIME_UNAVAILABLE"
        assert caught.value.status_code == 503
        assert env.sessions.list_turns(session.id) == []
    finally:
        await env.service.close()


@pytest.mark.asyncio
async def test_execute_turn_marks_error_and_releases_permit_on_admission_failure(
    env,
) -> None:
    session = _create_session(
        env.sessions, env.workspace_id, env.agent_id, env.model_id
    )
    env.runtime.fail_at = "admit_turn"
    try:
        with pytest.raises(DomainError) as caught:
            await env.service.execute_turn(
                session.id, parts=(RuntimeTextPart(text="hello"),)
            )

        assert caught.value.code == "RUNTIME_UNAVAILABLE"
        turn = env.sessions.list_turns(session.id)[0]
        assert turn.status is TurnStatus.error
        assert turn.error_code == "RUNTIME_UNAVAILABLE"
        assert env.runtime.released == [turn.id]
    finally:
        await env.service.close()


@pytest.mark.asyncio
async def test_execute_turn_releases_permit_when_runtime_session_fails(env) -> None:
    session = _create_session(
        env.sessions, env.workspace_id, env.agent_id, env.model_id
    )
    env.runtime.fail_at = "ensure_runtime_session"
    try:
        with pytest.raises(DomainError):
            await env.service.execute_turn(
                session.id, parts=(RuntimeTextPart(text="hello"),)
            )

        turn = env.sessions.list_turns(session.id)[0]
        assert turn.status is TurnStatus.error
        assert env.runtime.released == [turn.id]
        assert env.sessions.get(session.id).runtime_session_id is None
    finally:
        await env.service.close()


@pytest.mark.asyncio
async def test_execute_turn_replaces_stale_runtime_session_id(env) -> None:
    session = _create_session(
        env.sessions, env.workspace_id, env.agent_id, env.model_id
    )
    env.sessions.set_runtime_session_id(session.id, "rt_stale", 1_000)
    env.runtime.runtime_session_id = "rt_new"
    try:
        await env.service.execute_turn(
            session.id, parts=(RuntimeTextPart(text="hello"),)
        )

        ensure_call = next(
            call for call in env.runtime.calls if call[0] == "ensure_runtime_session"
        )
        assert ensure_call[1] == "rt_stale"
        assert env.sessions.get(session.id).runtime_session_id == "rt_new"
    finally:
        await env.service.close()


@pytest.mark.asyncio
async def test_execute_turn_rejects_file_parts_before_admission(env) -> None:
    session = _create_session(
        env.sessions, env.workspace_id, env.agent_id, env.model_id
    )
    try:
        with pytest.raises(DomainError) as caught:
            await env.service.execute_turn(
                session.id,
                parts=(
                    RuntimeFilePart(
                        relative_path="notes.txt",
                        mime="text/plain",
                        filename="notes.txt",
                    ),
                ),
            )

        assert caught.value.code == "VALIDATION_ERROR"
        assert caught.value.status_code == 400
        assert env.sessions.list_turns(session.id) == []
        assert env.runtime.calls == []
    finally:
        await env.service.close()


@pytest.mark.asyncio
async def test_turn_failed_event_sets_error_status(env) -> None:
    session = _create_session(
        env.sessions, env.workspace_id, env.agent_id, env.model_id
    )
    try:
        turn = await env.service.execute_turn(
            session.id, parts=(RuntimeTextPart(text="hello"),)
        )
        await env.runtime.events.put(_failed_event(env.workspace_id, turn.id))

        updated = turn
        for _ in range(100):
            updated = env.sessions.get_turn(turn.id)
            if updated.status is TurnStatus.error:
                break
            await asyncio.sleep(0.01)

        assert updated.status is TurnStatus.error
        assert updated.error_code == "MODEL_ERROR"
        assert updated.error_message == "model exploded"
        assert env.runtime.released == [turn.id]
    finally:
        await env.service.close()


@pytest.mark.asyncio
async def test_subscriber_lagged_interrupts_running_turn(env) -> None:
    session = _create_session(
        env.sessions, env.workspace_id, env.agent_id, env.model_id
    )
    try:
        turn = await env.service.execute_turn(
            session.id, parts=(RuntimeTextPart(text="hello"),)
        )
        await env.runtime.events.put(
            RuntimeResyncRequired(
                workspace_id=env.workspace_id,
                reason=RuntimeResyncReason.SUBSCRIBER_LAGGED,
                last_durable_seq=0,
            )
        )

        updated = turn
        for _ in range(100):
            updated = env.sessions.get_turn(turn.id)
            if updated.status is TurnStatus.interrupted:
                break
            await asyncio.sleep(0.01)

        assert updated.status is TurnStatus.interrupted
        assert updated.error_code == "RUNTIME_RESYNC_REQUIRED"
        assert env.runtime.released == [turn.id]
    finally:
        await env.service.close()


@pytest.mark.asyncio
async def test_runtime_exited_interrupts_running_turn(env) -> None:
    session = _create_session(
        env.sessions, env.workspace_id, env.agent_id, env.model_id
    )
    try:
        turn = await env.service.execute_turn(
            session.id, parts=(RuntimeTextPart(text="hello"),)
        )
        await env.runtime.events.put(
            RuntimeResyncRequired(
                workspace_id=env.workspace_id,
                reason=RuntimeResyncReason.RUNTIME_EXITED,
                last_durable_seq=0,
            )
        )

        updated = turn
        for _ in range(100):
            updated = env.sessions.get_turn(turn.id)
            if updated.status is TurnStatus.interrupted:
                break
            await asyncio.sleep(0.01)

        assert updated.status is TurnStatus.interrupted
        assert updated.error_code == "RUNTIME_RESYNC_REQUIRED"
        assert env.runtime.released == [turn.id]
    finally:
        await env.service.close()


@pytest.mark.asyncio
async def test_close_interrupts_running_turns_and_releases_permits(env) -> None:
    session = _create_session(
        env.sessions, env.workspace_id, env.agent_id, env.model_id
    )
    turn = await env.service.execute_turn(
        session.id, parts=(RuntimeTextPart(text="hello"),)
    )
    await env.service.close()

    assert env.sessions.get_turn(turn.id).status is TurnStatus.interrupted
    assert env.runtime.released == [turn.id]
