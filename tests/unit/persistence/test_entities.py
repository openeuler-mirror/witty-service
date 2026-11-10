from __future__ import annotations

import pytest
from sqlalchemy import UniqueConstraint, create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from witty_service.persistence.entities import (
    AgentDefinitionORM,
    Base,
    ModelORM,
    SessionORM,
    TurnOperationORM,
    TurnORM,
    TurnRuntimeMessageORM,
    WorkspaceORM,
    utc_ms_now,
)

EXPECTED_TABLES = {
    "agent_definitions",
    "mcp_servers",
    "models",
    "scheduled_task_runs",
    "scheduled_tasks",
    "sessions",
    "skill_repo",
    "skills",
    "turn_operations",
    "turn_runtime_messages",
    "turns",
    "workspaces",
}

EXPECTED_INDEXES = {
    "ix_agent_definitions_catalog",
    "ix_mcp_servers_enabled_name",
    "ix_models_catalog",
    "ix_scheduled_task_runs_global_created",
    "ix_scheduled_task_runs_lease",
    "ix_scheduled_task_runs_session",
    "ix_scheduled_task_runs_task_created",
    "ix_scheduled_task_runs_turn",
    "ix_scheduled_tasks_enabled",
    "ix_scheduled_tasks_workspace",
    "ix_sessions_scheduled_task",
    "ix_sessions_workspace_sidebar",
    "ix_skill_repo_status",
    "ix_skills_enabled_name",
    "ix_turn_operations_session_created",
    "ix_turn_operations_target",
    "ix_turns_agent",
    "ix_workspaces_sidebar",
    "uq_agent_definitions_one_primary",
    "uq_models_provider_endpoint_model",
    "uq_models_single_default",
    "uq_scheduled_task_runs_one_running",
    "uq_turn_operations_one_reverting",
    "uq_turns_one_running",
    "uq_workspaces_one_default",
}

EXPECTED_UNIQUE_CONSTRAINTS = {
    "uq_agent_definitions_name",
    "uq_mcp_servers_mcp_server_name",
    "uq_sessions_runtime_session_id",
    "uq_skill_repo_repo_name",
    "uq_skills_repo_path",
    "uq_skills_skill_name",
    "uq_turn_runtime_messages_turn_ordinal",
    "uq_turns_session_seq",
    "uq_workspaces_host_directory",
}


def _enable_foreign_keys(engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


@pytest.fixture
def engine():
    engine = create_engine("sqlite://")
    _enable_foreign_keys(engine)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db(engine):
    with Session(engine, expire_on_commit=False) as session:
        yield session


def _add_catalog(db: Session) -> tuple[AgentDefinitionORM, ModelORM, WorkspaceORM]:
    now = utc_ms_now()
    agent = AgentDefinitionORM(
        id="agent-1",
        kind="primary",
        name="Assistant",
        config_updated_at=now,
    )
    model = ModelORM(
        id="model-1",
        display_name="GPT",
        provider_id="openai",
        provider_model_id="gpt-5",
    )
    db.add_all([agent, model])
    db.flush()
    workspace = WorkspaceORM(
        id="workspace-1",
        name="Default",
        host_directory="/tmp/workspace",
        sandbox_type="local",
        default_agent_id=agent.id,
        default_model_id=model.id,
        last_activity_at=now,
    )
    db.add(workspace)
    db.flush()
    return agent, model, workspace


def _add_session(
    db: Session, workspace: WorkspaceORM, agent: AgentDefinitionORM, model: ModelORM
) -> SessionORM:
    session = SessionORM(
        id="session-1",
        workspace_id=workspace.id,
        current_agent_id=agent.id,
        current_model_id=model.id,
        last_activity_at=utc_ms_now(),
    )
    db.add(session)
    db.flush()
    return session


def _add_turn(
    db: Session, session: SessionORM, agent: AgentDefinitionORM, model: ModelORM
) -> TurnORM:
    turn = TurnORM(
        id="turn-1",
        session_id=session.id,
        turn_seq=1,
        agent_id=agent.id,
        model_id=model.id,
        agent_name_snapshot=agent.name,
        model_name_snapshot=model.display_name,
        started_at=utc_ms_now(),
    )
    db.add(turn)
    db.flush()
    return turn


def test_metadata_contains_only_the_new_schema() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    assert {
        index.name for table in Base.metadata.tables.values() for index in table.indexes
    } == EXPECTED_INDEXES
    assert {
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    } == EXPECTED_UNIQUE_CONSTRAINTS


def test_schema_builds_without_foreign_key_violations(engine) -> None:
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []


def test_session_turn_graph_cascades(db: Session) -> None:
    agent, model, workspace = _add_catalog(db)
    session = _add_session(db, workspace, agent, model)
    turn = _add_turn(db, session, agent, model)
    db.add(
        TurnOperationORM(
            id="operation-1",
            session_id=session.id,
            target_turn_id=turn.id,
        )
    )
    db.add(
        TurnRuntimeMessageORM(
            runtime_message_id="message-1",
            turn_id=turn.id,
            role="user",
            ordinal=0,
        )
    )
    db.commit()

    db.delete(session)
    db.commit()

    assert db.scalar(select(TurnORM).where(TurnORM.id == turn.id)) is None
    assert (
        db.scalar(select(TurnOperationORM).where(TurnOperationORM.id == "operation-1"))
        is None
    )
    assert (
        db.scalar(
            select(TurnRuntimeMessageORM).where(
                TurnRuntimeMessageORM.runtime_message_id == "message-1"
            )
        )
        is None
    )


def test_partial_unique_indexes(db: Session) -> None:
    agent, _, _ = _add_catalog(db)
    db.commit()
    db.add(
        AgentDefinitionORM(
            id="agent-2",
            kind="primary",
            name="Second",
            config_updated_at=utc_ms_now(),
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    agent.archived_at = utc_ms_now()
    db.commit()
    db.add(
        AgentDefinitionORM(
            id="agent-2",
            kind="primary",
            name="Second",
            config_updated_at=utc_ms_now(),
        )
    )
    db.commit()
