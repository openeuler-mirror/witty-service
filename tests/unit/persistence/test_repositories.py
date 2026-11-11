from __future__ import annotations

from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from witty_service.domain.enums import (
    AgentKind,
    AgentSourceType,
    ChannelAccessMode,
    ChannelConversationType,
    MessageRole,
    SandboxType,
    ScheduledTaskRunStatus,
    ScheduledTaskScheduleType,
    SessionOrigin,
    TurnOperationStatus,
    TurnStatus,
)
from witty_service.persistence.entities import Base
from witty_service.persistence.repository import (
    AgentRepository,
    CatalogRepository,
    ChannelRepository,
    SchedulerRepository,
    SessionRepository,
    WorkspaceRepository,
)


@dataclass
class Repos:
    agent: AgentRepository
    workspace: WorkspaceRepository
    catalog: CatalogRepository
    session: SessionRepository
    scheduler: SchedulerRepository
    channel: ChannelRepository


@pytest.fixture()
def repos():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield Repos(
        agent=AgentRepository(factory),
        workspace=WorkspaceRepository(factory),
        catalog=CatalogRepository(factory),
        session=SessionRepository(factory),
        scheduler=SchedulerRepository(factory),
        channel=ChannelRepository(factory),
    )
    engine.dispose()


def _workspace(repos: Repos) -> tuple[str, str]:
    agent = repos.agent.create(
        "Primary",
        AgentKind.primary,
        "default agent",
        "prompt",
        AgentSourceType.preset,
        None,
        1_000,
    )
    workspace = repos.workspace.create(
        "Default",
        "/work/default",
        SandboxType.local,
        agent.id,
        None,
        1,
        1_000,
    )
    return agent.id, workspace.id


def test_agent_configuration_uses_expected_version(repos: Repos) -> None:
    agent = repos.agent.create(
        "Primary",
        AgentKind.primary,
        "default",
        "prompt",
        AgentSourceType.preset,
        None,
        1_000,
    )
    assert (
        repos.agent.update_configuration(
            agent.id, 999, name="Stale", description=None, system_prompt=None, now=1_001
        )
        is None
    )
    updated = repos.agent.update_configuration(
        agent.id,
        1_000,
        name="Renamed",
        description="new",
        system_prompt=None,
        now=1_001,
    )
    assert updated is not None
    assert updated.name == "Renamed"
    assert updated.config_updated_at == 1_001
    with pytest.raises(ValueError):
        repos.agent.archive(agent.id, 1_002)


def test_workspace_default_switches_and_directory_is_immutable(repos: Repos) -> None:
    agent_id, first_id = _workspace(repos)
    second = repos.workspace.create(
        "Second", "/work/second", SandboxType.local, agent_id, None, 0, 2_000
    )
    changed = repos.workspace.set_default(second.id, 2_001)
    assert changed is not None and changed.is_default == 1
    assert repos.workspace.get(first_id).is_default == 0
    renamed = repos.workspace.update_metadata(first_id, "Renamed", 2_002)
    assert renamed is not None
    assert renamed.name == "Renamed"
    assert renamed.host_directory == "/work/default"


def test_session_tracks_counts_and_turn_sequence(repos: Repos) -> None:
    agent_id, workspace_id = _workspace(repos)
    model = repos.catalog.create_model(
        "Model", "provider", "model-1", None, None, None, 8192, 0.7, 1_000
    )
    session = repos.session.create(
        workspace_id,
        agent_id,
        model.id,
        SessionOrigin.web,
        None,
        None,
        None,
        1_100,
    )
    assert repos.workspace.get(workspace_id).session_count == 1

    first = repos.session.start_turn(session.id, agent_id, model.id, 1_101)
    assert first.turn_seq == 1
    assert first.agent_name_snapshot == "Primary"
    assert first.model_name_snapshot == "Model"
    repos.session.finish_turn(first.id, TurnStatus.completed, None, None, 1_102)

    second = repos.session.start_turn(session.id, agent_id, model.id, 1_103)
    assert second.turn_seq == 2
    assert repos.session.get(session.id).last_activity_at == 1_103
    assert repos.session.count_open_sessions(workspace_id) == 1

    assert repos.session.mark_deleted(session.id, 1_104) is True
    assert repos.session.mark_deleted(session.id, 1_105) is False
    assert repos.workspace.get(workspace_id).session_count == 0
    assert repos.session.count_open_sessions(workspace_id) == 0


def test_start_turn_rejects_unavailable_catalog_entries(repos: Repos) -> None:
    agent_id, workspace_id = _workspace(repos)
    model = repos.catalog.create_model(
        "Model", "provider", "model-1", None, None, None, 8192, 0.7, 1_000
    )
    session = repos.session.create(
        workspace_id, agent_id, model.id, SessionOrigin.web, None, None, None, 1_100
    )
    repos.catalog.disable_model(model.id, 1_101)
    with pytest.raises(LookupError):
        repos.session.start_turn(session.id, agent_id, model.id, 1_102)


def test_finish_turn_only_accepts_terminal_status(repos: Repos) -> None:
    agent_id, workspace_id = _workspace(repos)
    model = repos.catalog.create_model(
        "Model", "provider", "model-1", None, None, None, 8192, 0.7, 1_000
    )
    session = repos.session.create(
        workspace_id, agent_id, model.id, SessionOrigin.web, None, None, None, 1_100
    )
    turn = repos.session.start_turn(session.id, agent_id, model.id, 1_101)
    with pytest.raises(ValueError):
        repos.session.finish_turn(turn.id, TurnStatus.running, None, None, 1_102)
    finished = repos.session.finish_turn(
        turn.id, TurnStatus.error, "provider_error", "failed", 1_102
    )
    assert finished is not None and finished.error_code == "provider_error"
    assert (
        repos.session.finish_turn(turn.id, TurnStatus.completed, None, None, 1_103)
        is None
    )


def test_catalog_default_model_and_skill_upsert(repos: Repos) -> None:
    first = repos.catalog.create_model(
        "First", "p", "m1", None, None, None, 1024, 0.2, 1_000
    )
    second = repos.catalog.create_model(
        "Second", "p", "m2", None, None, None, 1024, 0.2, 1_001
    )
    repos.catalog.set_default_model(first.id, 1_002)
    assert repos.catalog.get_default_model().id == first.id
    repos.catalog.set_default_model(second.id, 1_003)
    assert repos.catalog.get_default_model().id == second.id
    repos.catalog.disable_model(second.id, 1_004)
    assert repos.catalog.get_default_model() is None

    repo = repos.catalog.create_skill_repo(
        "builtin", "builtin", None, None, "/skills", 1_005
    )
    skill = repos.catalog.upsert_skill(
        repo.repo_id, "demo", "demo/SKILL.md", "hash-1", {}, None, None, 1, 1_006
    )
    again = repos.catalog.upsert_skill(
        repo.repo_id, "demo", "demo/SKILL.md", "hash-2", {}, None, None, 0, 1_007
    )
    assert again.skill_id == skill.skill_id
    assert again.content_sha256 == "hash-2"
    assert again.enabled == 0
    assert repos.catalog.get_skill_repo(repo.repo_id).skill_count == 1


def test_mcp_upsert_increments_secret_revision(repos: Repos) -> None:
    first = repos.catalog.upsert_mcp_server("Search", {"url": "a"}, "ref-1", 1, 1_000)
    second = repos.catalog.upsert_mcp_server("search", {"url": "b"}, "ref-2", 1, 1_001)
    third = repos.catalog.upsert_mcp_server("search", {"url": "c"}, "ref-2", 1, 1_002)
    assert first.secret_revision == 0
    assert second.id == first.id
    assert second.secret_revision == 1
    assert third.secret_revision == 1
    assert second.mcp_server_name == "Search"


def test_scheduler_run_finish_updates_task_summary(repos: Repos) -> None:
    agent_id, workspace_id = _workspace(repos)
    model = repos.catalog.create_model(
        "Model", "provider", "model-1", None, None, None, 8192, 0.7, 1_000
    )
    task = repos.scheduler.create_task(
        "Daily",
        ScheduledTaskScheduleType.cron,
        "UTC",
        "content",
        workspace_id,
        agent_id,
        model.id,
        "0 0 * * *",
        None,
        1_000,
    )
    run = repos.scheduler.create_run(task.id, "worker-1", 2_000, None, None, 1_001)
    finished = repos.scheduler.finish_run(
        run.id, ScheduledTaskRunStatus.succeeded, None, "worker-1", 1_002
    )
    assert finished is not None and finished.lease_owner is None
    task_after = repos.scheduler.get_task(task.id)
    assert task_after.last_run_id == run.id
    assert task_after.last_run_at == run.created_at
    assert task_after.last_run_status is ScheduledTaskRunStatus.succeeded
    assert (
        repos.scheduler.finish_run(
            run.id, ScheduledTaskRunStatus.failed, None, "worker-2", 1_003
        )
        is None
    )


def test_channel_event_dedup_and_route_identity(repos: Repos) -> None:
    agent_id, workspace_id = _workspace(repos)
    model = repos.catalog.create_model(
        "Model", "provider", "model-1", None, None, None, 8192, 0.7, 1_000
    )
    instance = repos.channel.create_instance(
        "feishu",
        workspace_id,
        agent_id,
        model.id,
        "Feishu",
        None,
        {},
        None,
        None,
        1_000,
    )
    assert repos.channel.record_inbound_event(instance.id, "evt-1", 1_001) is True
    assert repos.channel.record_inbound_event(instance.id, "evt-1", 1_002) is False

    route = repos.channel.get_or_create_route(
        instance.id, ChannelConversationType.direct, "user-1", 1_003
    )
    again = repos.channel.get_or_create_route(
        instance.id, ChannelConversationType.direct, "user-1", 1_004
    )
    assert again.id == route.id
    updated = repos.channel.set_route_session(
        route.id, "session-1", "placeholder-1", 1_005
    )
    assert updated is not None and updated.active_session_id == "session-1"

    policy = repos.channel.upsert_access_policy(
        instance.id,
        ChannelConversationType.direct,
        ChannelAccessMode.allowlist,
        ["user-1"],
        1,
        1_006,
    )
    assert policy.mode == ChannelAccessMode.allowlist


def test_turn_operation_lifecycle(repos: Repos) -> None:
    agent_id, workspace_id = _workspace(repos)
    model = repos.catalog.create_model(
        "Model", "provider", "model-1", None, None, None, 8192, 0.7, 1_000
    )
    session = repos.session.create(
        workspace_id, agent_id, model.id, SessionOrigin.web, None, None, None, 1_100
    )
    turn = repos.session.start_turn(session.id, agent_id, model.id, 1_101)
    repos.session.finish_turn(turn.id, TurnStatus.completed, None, None, 1_102)
    operation = repos.session.create_operation(session.id, turn.id, 1_103)
    with pytest.raises(ValueError):
        repos.session.finish_operation(
            operation.id, TurnOperationStatus.reverting, 1_104
        )
    finished = repos.session.finish_operation(
        operation.id, TurnOperationStatus.accepted, 1_104
    )
    assert finished is not None and finished.status == TurnOperationStatus.accepted
    assert (
        repos.session.finish_operation(operation.id, TurnOperationStatus.failed, 1_105)
        is None
    )


def test_runtime_message_mapping_is_idempotent(repos: Repos) -> None:
    agent_id, workspace_id = _workspace(repos)
    model = repos.catalog.create_model(
        "Model", "provider", "model-1", None, None, None, 8192, 0.7, 1_000
    )
    session = repos.session.create(
        workspace_id, agent_id, model.id, SessionOrigin.web, None, None, None, 1_100
    )
    turn = repos.session.start_turn(session.id, agent_id, model.id, 1_101)
    first = repos.session.add_runtime_message(
        "msg-1", turn.id, MessageRole.user, 0, 1_102
    )
    again = repos.session.add_runtime_message(
        "msg-1", turn.id, MessageRole.user, 0, 1_103
    )
    assert again == first
    with pytest.raises(ValueError):
        repos.session.add_runtime_message(
            "msg-1", turn.id, MessageRole.assistant, 0, 1_104
        )
