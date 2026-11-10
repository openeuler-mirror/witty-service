from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from witty_service.domain.enums import (
    AgentKind,
    ChannelProvisioningStatus,
    MessageRole,
    SandboxType,
    ScheduledTaskRunStatus,
    ScheduledTaskScheduleType,
    SessionOrigin,
    TurnOperationStatus,
    TurnStatus,
)
from witty_service.persistence.db import create_session_factory, create_sqlite_engine
from witty_service.persistence.entities import Base, utc_ms_now
from witty_service.persistence.repository import (
    AgentRepository,
    CatalogRepository,
    ChannelRepository,
    SchedulerRepository,
    SessionRepository,
    WorkspaceRepository,
)


@dataclass(frozen=True, slots=True)
class Fixture:
    engine: Engine
    factory: sessionmaker[Session]
    now: int
    agents: AgentRepository
    sessions: SessionRepository
    scheduler: SchedulerRepository
    channels: ChannelRepository
    primary_id: str
    model_id: str
    workspace_id: str


@pytest.fixture()
def fixture(tmp_path: Path) -> Iterator[Fixture]:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'constraints.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    now = utc_ms_now()
    agents = AgentRepository(factory)
    primary = agents.create("Primary", AgentKind.primary, now)
    model = CatalogRepository(factory).create_model(
        "Model", "provider", "model-1", None, None, None, 8192, 0.7, now
    )
    workspace = WorkspaceRepository(factory).create(
        "Default",
        str(tmp_path / "workspace"),
        SandboxType.local,
        primary.id,
        model.id,
        1,
        now,
    )
    yield Fixture(
        engine=engine,
        factory=factory,
        now=now,
        agents=agents,
        sessions=SessionRepository(factory),
        scheduler=SchedulerRepository(factory),
        channels=ChannelRepository(factory),
        primary_id=primary.id,
        model_id=model.id,
        workspace_id=workspace.id,
    )
    engine.dispose()


def test_only_one_active_primary_and_replacement_after_archive(
    fixture: Fixture,
) -> None:
    with pytest.raises(IntegrityError):
        fixture.agents.create("Second", AgentKind.primary, fixture.now)

    with fixture.engine.begin() as connection:
        connection.execute(
            text("update agent_definitions set archived_at = 1 where id = :id"),
            {"id": fixture.primary_id},
        )
    replacement = fixture.agents.create("Second", AgentKind.primary, fixture.now + 1)

    primary = fixture.agents.get_primary()
    assert primary is not None
    assert primary.id == replacement.id


def test_only_one_default_workspace(fixture: Fixture) -> None:
    with pytest.raises(IntegrityError):
        WorkspaceRepository(fixture.factory).create(
            "Other",
            "/tmp/other-workspace",
            SandboxType.local,
            fixture.primary_id,
            fixture.model_id,
            1,
            fixture.now,
        )


def test_model_endpoint_uniqueness_treats_null_and_empty_base_url_equally(
    fixture: Fixture,
) -> None:
    catalog = CatalogRepository(fixture.factory)
    catalog.create_model(
        "Null URL", "provider", "duplicate", None, None, None, 8192, 0.7, fixture.now
    )

    with pytest.raises(IntegrityError):
        catalog.create_model(
            "Empty URL", "provider", "duplicate", None, "", None, 8192, 0.7, fixture.now
        )

    other = catalog.create_model(
        "Other URL",
        "provider",
        "duplicate",
        None,
        "https://example.com",
        None,
        8192,
        0.7,
        fixture.now,
    )
    assert other.id


def test_session_delete_cascades_turns_operations_messages_and_nulls_task_runs(
    fixture: Fixture,
) -> None:
    session = fixture.sessions.create(
        fixture.workspace_id,
        fixture.primary_id,
        fixture.model_id,
        SessionOrigin.web,
        "Session",
        None,
        None,
        fixture.now,
    )
    turn = fixture.sessions.start_turn(
        session.id, fixture.primary_id, fixture.model_id, fixture.now
    )
    operation = fixture.sessions.create_operation(session.id, turn.id, fixture.now)
    fixture.sessions.add_runtime_message(
        "msg-1", turn.id, MessageRole.user, 1, fixture.now
    )
    task = fixture.scheduler.create_task(
        "Task",
        ScheduledTaskScheduleType.cron,
        "Asia/Shanghai",
        "content",
        fixture.workspace_id,
        fixture.primary_id,
        fixture.model_id,
        "0 * * * *",
        None,
        fixture.now,
    )
    run = fixture.scheduler.create_run(
        task.id, "lease", fixture.now + 60_000, session.id, turn.id, fixture.now
    )

    assert fixture.sessions.get_turn(turn.id) is not None
    assert fixture.sessions.get_operation(operation.id) is not None
    assert len(fixture.sessions.list_runtime_messages(turn.id)) == 1

    with fixture.engine.begin() as connection:
        connection.execute(
            text("delete from sessions where id = :id"), {"id": session.id}
        )

    assert fixture.sessions.get_turn(turn.id) is None
    assert fixture.sessions.list_runtime_messages(turn.id) == []
    assert fixture.sessions.get_operation(operation.id) is None
    stored_run = fixture.scheduler.get_run(run.id)
    assert stored_run is not None
    assert stored_run.session_id is None
    assert stored_run.turn_id is None


def test_only_one_default_model(fixture: Fixture) -> None:
    catalog = CatalogRepository(fixture.factory)
    catalog.set_default_model(fixture.model_id, fixture.now)
    other = catalog.create_model(
        "Other", "provider", "model-2", None, None, None, 8192, 0.7, fixture.now
    )

    with pytest.raises(IntegrityError), fixture.engine.begin() as connection:
        connection.execute(
            text("update models set is_default = 1 where id = :id"), {"id": other.id}
        )


def test_agent_and_model_deletes_are_restricted(fixture: Fixture) -> None:
    with pytest.raises(IntegrityError), fixture.engine.begin() as connection:
        connection.execute(
            text("delete from agent_definitions where id = :id"),
            {"id": fixture.primary_id},
        )
    with pytest.raises(IntegrityError), fixture.engine.begin() as connection:
        connection.execute(
            text("delete from models where id = :id"), {"id": fixture.model_id}
        )


def test_only_one_running_scheduled_run_per_task(fixture: Fixture) -> None:
    task = fixture.scheduler.create_task(
        "Task",
        ScheduledTaskScheduleType.interval,
        "UTC",
        "content",
        fixture.workspace_id,
        fixture.primary_id,
        fixture.model_id,
        None,
        3600,
        fixture.now,
    )
    first = fixture.scheduler.create_run(
        task.id, "lease-1", fixture.now + 60_000, None, None, fixture.now
    )

    with pytest.raises(IntegrityError):
        fixture.scheduler.create_run(
            task.id, "lease-2", fixture.now + 60_000, None, None, fixture.now + 1
        )

    fixture.scheduler.finish_run(
        first.id, ScheduledTaskRunStatus.succeeded, None, "lease-1", fixture.now + 2
    )
    second = fixture.scheduler.create_run(
        task.id, "lease-2", fixture.now + 60_000, None, None, fixture.now + 3
    )
    assert second.id != first.id


def test_finish_run_requires_matching_lease_owner(fixture: Fixture) -> None:
    task = fixture.scheduler.create_task(
        "Task",
        ScheduledTaskScheduleType.interval,
        "UTC",
        "content",
        fixture.workspace_id,
        fixture.primary_id,
        fixture.model_id,
        None,
        3600,
        fixture.now,
    )
    run = fixture.scheduler.create_run(
        task.id, "lease-1", fixture.now + 60_000, None, None, fixture.now
    )

    assert (
        fixture.scheduler.finish_run(
            run.id,
            ScheduledTaskRunStatus.succeeded,
            None,
            "lease-2",
            fixture.now + 1,
        )
        is None
    )
    stored = fixture.scheduler.get_run(run.id)
    assert stored is not None
    assert stored.status is ScheduledTaskRunStatus.running
    assert stored.finished_at is None

    finished = fixture.scheduler.finish_run(
        run.id, ScheduledTaskRunStatus.succeeded, None, "lease-1", fixture.now + 2
    )
    assert finished is not None
    assert finished.status is ScheduledTaskRunStatus.succeeded


def test_waiting_channel_provisioning_is_unique_per_channel_and_owner(
    fixture: Fixture,
) -> None:
    def create(owner_ref: str | None, now: int) -> str:
        return fixture.channels.create_provisioning(
            "wecom",
            owner_ref,
            fixture.workspace_id,
            fixture.primary_id,
            fixture.model_id,
            now + 600_000,
            None,
            2_000,
            None,
            now,
        ).id

    first = create(None, fixture.now)
    with pytest.raises(IntegrityError):
        create(None, fixture.now + 1)
    create("owner-2", fixture.now + 2)

    fixture.channels.finish_provisioning(
        first, ChannelProvisioningStatus.expired, None, fixture.now + 3
    )

    assert create(None, fixture.now + 4)


def test_only_one_reverting_turn_operation(fixture: Fixture) -> None:
    session = fixture.sessions.create(
        fixture.workspace_id,
        fixture.primary_id,
        fixture.model_id,
        SessionOrigin.web,
        None,
        None,
        None,
        fixture.now,
    )
    turn = fixture.sessions.start_turn(
        session.id, fixture.primary_id, fixture.model_id, fixture.now
    )
    first = fixture.sessions.create_operation(session.id, turn.id, fixture.now)

    with pytest.raises(IntegrityError):
        fixture.sessions.create_operation(session.id, turn.id, fixture.now + 1)

    fixture.sessions.finish_operation(
        first.id, TurnOperationStatus.accepted, fixture.now + 2
    )
    second = fixture.sessions.create_operation(session.id, turn.id, fixture.now + 3)
    assert second.id != first.id


def test_concurrent_turn_start_allows_single_running_turn(fixture: Fixture) -> None:
    session = fixture.sessions.create(
        fixture.workspace_id,
        fixture.primary_id,
        fixture.model_id,
        SessionOrigin.web,
        None,
        None,
        None,
        fixture.now,
    )
    barrier = threading.Barrier(6)
    results: list[object] = []
    lock = threading.Lock()

    def start() -> None:
        barrier.wait()
        try:
            outcome: object = fixture.sessions.start_turn(
                session.id, fixture.primary_id, fixture.model_id, fixture.now
            ).id
        except Exception as error:
            outcome = error
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=start) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 6
    winners = [result for result in results if isinstance(result, str)]
    failures = [result for result in results if not isinstance(result, str)]
    assert len(winners) == 1
    assert all(isinstance(error, IntegrityError) for error in failures)

    turns = fixture.sessions.list_turns(session.id)
    assert len(turns) == 1
    assert turns[0].turn_seq == 1

    fixture.sessions.finish_turn(
        turns[0].id, TurnStatus.completed, None, None, fixture.now
    )
    next_turn = fixture.sessions.start_turn(
        session.id, fixture.primary_id, fixture.model_id, fixture.now + 1
    )
    assert next_turn.turn_seq == 2


def test_running_turn_uniqueness_is_per_session(fixture: Fixture) -> None:
    first = fixture.sessions.create(
        fixture.workspace_id,
        fixture.primary_id,
        fixture.model_id,
        SessionOrigin.web,
        None,
        None,
        None,
        fixture.now,
    )
    second = fixture.sessions.create(
        fixture.workspace_id,
        fixture.primary_id,
        fixture.model_id,
        SessionOrigin.web,
        None,
        None,
        None,
        fixture.now,
    )

    first_turn = fixture.sessions.start_turn(
        first.id, fixture.primary_id, fixture.model_id, fixture.now
    )
    second_turn = fixture.sessions.start_turn(
        second.id, fixture.primary_id, fixture.model_id, fixture.now
    )

    assert first_turn.turn_seq == 1
    assert second_turn.turn_seq == 1
