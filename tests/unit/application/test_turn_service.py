from __future__ import annotations

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


@pytest.fixture()
def service():
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
    service = TurnService(sessions=sessions, agents=agents, catalog=catalog)
    yield service, sessions, agents, catalog, workspace.id, agent.id, model.id
    engine.dispose()


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
    turns, sessions, *_ , workspace_id, agent_id, model_id = service
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
