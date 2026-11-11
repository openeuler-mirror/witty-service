from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from witty_service.application.history_service import HistoryService, SessionResyncView
from witty_service.domain.enums import (
    AgentKind,
    MessageRole,
    SandboxType,
    SessionOrigin,
    TurnStatus,
)
from witty_service.domain.errors import DomainError
from witty_service.persistence.entities import Base
from witty_service.persistence.repository import (
    AgentRepository,
    CatalogRepository,
    SessionRepository,
    WorkspaceRepository,
)
from witty_service.runtime.contracts import (
    HistoryPage,
    RuntimeErrorPayload,
    RuntimeEventEnvelope,
    RuntimeEventType,
    RuntimeHistoryMessage,
    RuntimeMessageDeltaPayload,
    RuntimeMessageKind,
    RuntimeReasoningPart,
    RuntimeTextPart,
    RuntimeToolPart,
    RuntimeToolStatus,
)
from witty_service.runtime.errors import RuntimeUnavailableError
from witty_service.runtime.events import (
    RuntimeResyncReason,
    RuntimeResyncRequired,
    RuntimeTurnEvent,
)


class FakeRuntime:
    def __init__(self) -> None:
        self.queries = []
        self.page = HistoryPage(items=())
        self.error: Exception | None = None
        self.stream: list[object] = []

    async def read_history(self, query):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        return self.page

    async def subscribe_events(self, workspace_id: str):
        for item in self.stream:
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
    service = HistoryService(sessions=sessions, runtime=runtime)
    yield SimpleNamespace(
        service=service,
        sessions=sessions,
        workspace_id=workspace.id,
        agent_id=agent.id,
        model_id=model.id,
        runtime=runtime,
    )
    engine.dispose()


def _session(env, *, runtime_session_id: str | None = "auto") -> str:
    session = env.sessions.create(
        env.workspace_id,
        env.agent_id,
        env.model_id,
        SessionOrigin.web,
        None,
        None,
        None,
        1_000,
    )
    if runtime_session_id == "auto":
        runtime_session_id = f"rt_{session.id}"
    if runtime_session_id is not None:
        env.sessions.set_runtime_session_id(session.id, runtime_session_id, 1_001)
    return session.id


def _history_message(
    message_id: str, kind: RuntimeMessageKind, *, created_at: int
) -> RuntimeHistoryMessage:
    parts = ()
    if kind is RuntimeMessageKind.ASSISTANT:
        parts = (
            RuntimeTextPart(text="answer"),
            RuntimeReasoningPart(text="think"),
            RuntimeToolPart(
                call_id="call_1", name="search", status=RuntimeToolStatus.COMPLETED
            ),
        )
    elif kind is RuntimeMessageKind.USER:
        parts = (RuntimeTextPart(text="question"),)
    return RuntimeHistoryMessage(
        message_id=message_id, kind=kind, created_at=created_at, parts=parts
    )


@pytest.mark.asyncio
async def test_list_messages_projects_history_and_attribution(env) -> None:
    session_id = _session(env)
    turn = env.sessions.start_turn(session_id, env.agent_id, env.model_id, 1_100)
    env.sessions.add_runtime_message(
        "msg_assistant", turn.id, MessageRole.assistant, 1, 1_100
    )
    env.sessions.finish_turn(turn.id, TurnStatus.completed, None, None, 1_200)
    env.runtime.page = HistoryPage(
        items=(
            _history_message(
                "msg_assistant", RuntimeMessageKind.ASSISTANT, created_at=1_100
            ),
            _history_message(
                "switch_1", RuntimeMessageKind.AGENT_SWITCHED, created_at=1_000
            ),
            _history_message("msg_user", RuntimeMessageKind.USER, created_at=900),
        ),
        next_cursor="rt_1:next_1",
        previous_cursor=None,
    )

    page = await env.service.list_messages(session_id, limit=20, cursor=None)

    assert [
        (item.message.message_id, item.turn.id if item.turn else None)
        for item in page.items
    ] == [
        ("msg_assistant", turn.id),
        ("msg_user", None),
    ]
    assert page.next_cursor is not None
    assert page.previous_cursor is None

    await env.service.list_messages(session_id, limit=20, cursor=page.next_cursor)
    assert env.runtime.queries[-1].cursor == "rt_1:next_1"


@pytest.mark.asyncio
async def test_list_messages_is_empty_without_runtime_session(env) -> None:
    session_id = _session(env, runtime_session_id=None)

    page = await env.service.list_messages(session_id, limit=20, cursor=None)

    assert page.items == ()
    assert env.runtime.queries == []


@pytest.mark.asyncio
async def test_list_messages_maps_runtime_unavailable(env) -> None:
    session_id = _session(env)
    env.runtime.error = RuntimeUnavailableError(workspace_id=env.workspace_id)

    with pytest.raises(RuntimeUnavailableError):
        await env.service.list_messages(session_id, limit=20, cursor=None)


@pytest.mark.asyncio
async def test_list_messages_rejects_foreign_and_invalid_cursor(env) -> None:
    first_id = _session(env)
    second_id = _session(env)
    env.runtime.page = HistoryPage(
        items=(), next_cursor="rt_1:next_1", previous_cursor=None
    )
    page = await env.service.list_messages(first_id, limit=20, cursor=None)

    with pytest.raises(DomainError) as cross:
        await env.service.list_messages(second_id, limit=20, cursor=page.next_cursor)
    assert cross.value.code == "CURSOR_SESSION_MISMATCH"
    assert cross.value.status_code == 400

    with pytest.raises(DomainError) as invalid:
        await env.service.list_messages(first_id, limit=20, cursor="broken")
    assert invalid.value.code == "INVALID_CURSOR"


@pytest.mark.asyncio
async def test_stream_events_filters_turns_and_maps_resync(env) -> None:
    session_id = _session(env)
    other_id = _session(env)
    turn = env.sessions.start_turn(session_id, env.agent_id, env.model_id, 1_100)
    other_turn = env.sessions.start_turn(other_id, env.agent_id, env.model_id, 1_100)

    def event(workspace_id, turn_id, event_type, message_id=None):
        payload = (
            RuntimeMessageDeltaPayload(delta="chunk")
            if event_type is RuntimeEventType.MESSAGE_DELTA
            else RuntimeMessageDeltaPayload(delta="other")
        )
        return RuntimeTurnEvent(
            workspace_id=workspace_id,
            turn_id=turn_id,
            event=RuntimeEventEnvelope(
                workspace_id=workspace_id,
                runtime_session_id="rt_1",
                event_id=f"evt_{turn_id}",
                type=event_type,
                durable_seq=None,
                occurred_at=1_100,
                payload=payload,
                runtime_message_id=message_id,
            ),
        )

    env.runtime.stream = [
        event(env.workspace_id, turn.id, RuntimeEventType.MESSAGE_DELTA, "msg_1"),
        event(env.workspace_id, other_turn.id, RuntimeEventType.MESSAGE_DELTA, "msg_2"),
        RuntimeResyncRequired(
            workspace_id=env.workspace_id,
            reason=RuntimeResyncReason.SUBSCRIBER_LAGGED,
            last_durable_seq=7,
        ),
    ]

    items = [item async for item in await env.service.stream_events(session_id)]

    assert [type(item).__name__ for item in items] == [
        "SessionEventView",
        "SessionResyncView",
    ]
    assert items[0].type == "message.delta"
    assert items[0].turn_id == turn.id
    assert items[0].payload == {"delta": "chunk"}
    assert isinstance(items[1], SessionResyncView)
    assert items[1].last_durable_seq == 7


@pytest.mark.asyncio
async def test_stream_forwards_session_level_error_by_runtime_session(env) -> None:
    session_id = _session(env, runtime_session_id="rt_current")
    other_id = _session(env, runtime_session_id="rt_other")
    event = RuntimeTurnEvent(
        workspace_id=env.workspace_id,
        turn_id=None,
        event=RuntimeEventEnvelope(
            workspace_id=env.workspace_id,
            runtime_session_id="rt_current",
            event_id="evt_error",
            type=RuntimeEventType.SESSION_ERROR,
            durable_seq=8,
            occurred_at=1_200,
            payload=RuntimeErrorPayload(code="provider_error", message="failed"),
        ),
    )
    env.runtime.stream = [event]

    items = [item async for item in await env.service.stream_events(session_id)]
    other_items = [item async for item in await env.service.stream_events(other_id)]

    assert [item.type for item in items] == ["runtime.status"]
    assert items[0].turn_id is None
    assert items[0].payload == {
        "code": "provider_error",
        "message": "failed",
        "source": "runtime",
        "retryable": False,
        "status": "error",
    }
    assert other_items == []


@pytest.mark.asyncio
async def test_stream_resolves_runtime_session_bound_after_subscribe(env) -> None:
    session_id = _session(env, runtime_session_id=None)
    event = RuntimeTurnEvent(
        workspace_id=env.workspace_id,
        turn_id=None,
        event=RuntimeEventEnvelope(
            workspace_id=env.workspace_id,
            runtime_session_id="rt_late",
            event_id="evt_late",
            type=RuntimeEventType.SESSION_ERROR,
            durable_seq=9,
            occurred_at=1_300,
            payload=RuntimeErrorPayload(code="provider_error", message="failed"),
        ),
    )
    env.runtime.stream = [event]

    stream = await env.service.stream_events(session_id)
    env.sessions.set_runtime_session_id(session_id, "rt_late", 1_200)
    items = [item async for item in stream]

    assert [item.type for item in items] == ["runtime.status"]
    assert items[0].session_id == session_id
