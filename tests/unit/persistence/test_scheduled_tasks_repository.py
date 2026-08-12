from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from witty_service.domain.enums import AgentStatus
from witty_service.persistence.db import (
    create_session_factory,
    create_sqlite_engine,
    init_db,
)
from witty_service.persistence.repositories import SqliteRepository


@pytest.fixture()
def repo() -> SqliteRepository:
    engine = create_sqlite_engine("sqlite:///:memory:")
    init_db(engine, auto_create=True)
    factory = create_session_factory(engine)
    try:
        yield SqliteRepository(factory)
    finally:
        engine.dispose()


def _create_agent(
    repo: SqliteRepository,
    agent_id: str = "agent-1",
    *,
    status: AgentStatus = AgentStatus.running,
) -> None:
    repo.create_agent_with_id(
        agent_id=agent_id,
        name="Demo Agent",
        description="demo",
        sandbox_type="local_process",
        adapter_type="http",
        workspace_path=f"/tmp/ws-{agent_id}",
        idle_timeout_seconds=300,
        status=status,
    )


def _create_task(
    repo: SqliteRepository,
    *,
    agent_id: str = "agent-1",
    name: str = "每日竞品追踪",
    **overrides: object,
):
    data: dict[str, object] = dict(
        name=name,
        schedule_type="cron",
        cron_expr="0 9 * * *",
        interval_seconds=None,
        timezone="Asia/Shanghai",
        content="追踪竞品动态并输出报告",
        agent_id=agent_id,
    )
    data.update(overrides)
    return repo.create_scheduled_task(**data)  # type: ignore[arg-type]


def test_scheduled_task_crud(repo: SqliteRepository) -> None:
    _create_agent(repo)
    task = _create_task(repo)

    fetched = repo.get_scheduled_task(task.id)
    assert fetched is not None
    assert fetched.name == "每日竞品追踪"
    assert fetched.schedule_type == "cron"
    assert fetched.enabled is True

    assert repo.get_scheduled_task("missing") is None
    assert [t.id for t in repo.list_scheduled_tasks()] == [task.id]
    assert [t.id for t in repo.list_scheduled_tasks(agent_id="agent-1")] == [task.id]
    assert repo.list_scheduled_tasks(agent_id="agent-other") == []

    updated = repo.update_scheduled_task(
        task.id,
        name="每日报表",
        schedule_type="interval",
        cron_expr=None,
        interval_seconds=3600,
        timezone="UTC",
        content="生成报表",
        workspace_folder=None,
        enabled=False,
    )
    assert updated.name == "每日报表"
    assert updated.schedule_type == "interval"
    assert updated.interval_seconds == 3600
    assert updated.enabled is False

    toggled = repo.set_scheduled_task_enabled(task.id, True)
    assert toggled.enabled is True


def test_scheduled_task_agent_flag_and_count(repo: SqliteRepository) -> None:
    _create_agent(repo)
    _create_task(repo)
    _create_task(repo, name="每日报表")

    assert repo.count_scheduled_tasks_for_agent("agent-1") == 2
    assert repo.count_scheduled_tasks_for_agent("missing") == 0

    agent = repo.update_agent_has_scheduled_tasks("agent-1", True)
    assert agent.has_scheduled_tasks is True


def test_delete_agent_cascades_scheduled_tasks(repo: SqliteRepository) -> None:
    _create_agent(repo)
    task = _create_task(repo)

    repo.delete_agent("agent-1")

    assert repo.get_scheduled_task(task.id) is None


def test_delete_scheduled_task_cascades_runs(repo: SqliteRepository) -> None:
    _create_agent(repo)
    task = _create_task(repo)
    run = repo.create_scheduled_task_run(task_id=task.id, status="running")

    repo.delete_scheduled_task(task.id)

    assert repo.get_scheduled_task_run(run.id) is None


def test_mark_session_scheduled_sets_provenance(repo: SqliteRepository) -> None:
    _create_agent(repo)
    task = _create_task(repo)
    session = repo.create_session("agent-1")

    marked = repo.mark_session_scheduled(session.id, task.id)

    assert marked.scheduled_task_id == task.id
    assert repo.get_session(session.id).scheduled_task_id == task.id

    summaries = repo.list_sessions_with_summary("agent-1")
    assert summaries[0]["scheduled_task_id"] == task.id

    agents = repo.list_agents_with_conversations()
    conv = agents[0]["conversations"][0]
    assert conv["scheduled_task_id"] == task.id


def test_mark_session_scheduled_missing_session_raises(repo: SqliteRepository) -> None:
    _create_agent(repo)
    task = _create_task(repo)

    with pytest.raises(KeyError):
        repo.mark_session_scheduled("missing-session", task.id)


def test_delete_scheduled_task_cascades_sessions(repo: SqliteRepository) -> None:
    """删除任务时，归属该任务的执行会话（及其消息）一并级联删除。"""
    _create_agent(repo)
    task = _create_task(repo)
    session = repo.create_session("agent-1")
    repo.mark_session_scheduled(session.id, task.id)

    repo.delete_scheduled_task(task.id)

    assert repo.get_session(session.id) is None
    assert repo.list_sessions_with_summary("agent-1") == []


def test_scheduled_task_run_crud_and_ordering(repo: SqliteRepository) -> None:
    _create_agent(repo)
    task = _create_task(repo)

    run_1 = repo.create_scheduled_task_run(task_id=task.id, status="running")
    run_2 = repo.create_scheduled_task_run(task_id=task.id, status="succeeded")

    assert repo.get_scheduled_task_run(run_1.id) is not None
    assert [r.id for r in repo.list_scheduled_task_runs(task.id)] == [
        run_2.id,
        run_1.id,
    ]

    updated = repo.update_scheduled_task_run(
        run_1.id,
        status="failed",
        session_id="session-1",
        error="boom",
        started_at=run_1.started_at,
        finished_at=datetime.now(UTC),
    )
    assert updated.status == "failed"
    assert updated.session_id == "session-1"
    assert updated.error == "boom"


def test_list_scheduled_task_runs_drops_session_id_for_deleted_session(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo)
    task = _create_task(repo)
    session = repo.create_session("agent-1")
    run = repo.create_scheduled_task_run(task_id=task.id, status="running")
    repo.update_scheduled_task_run(
        run.id,
        status="succeeded",
        session_id=session.id,
        error=None,
        started_at=None,
        finished_at=None,
    )

    assert repo.list_scheduled_task_runs(task.id)[0].session_id == session.id

    repo.delete_session(session.id)

    assert repo.list_scheduled_task_runs(task.id)[0].session_id is None


def test_list_scheduled_task_runs_drops_dangling_session_id(
    repo: SqliteRepository,
) -> None:
    """运行记录引用的会话已不存在时，列表不再返回失效的 session_id。"""
    _create_agent(repo)
    task = _create_task(repo)
    run = repo.create_scheduled_task_run(task_id=task.id, status="succeeded")
    repo.update_scheduled_task_run(
        run.id,
        status="succeeded",
        session_id="ghost-session",
        error=None,
        started_at=None,
        finished_at=None,
    )

    listed = repo.list_scheduled_task_runs(task.id)
    assert listed[0].session_id is None


def test_list_scheduled_task_runs_by_task_ids_groups_and_orders(
    repo: SqliteRepository,
) -> None:
    """聚合查询：每个任务返回各自最近 N 条（created_at 降序），空任务返回空列表。"""
    _create_agent(repo)
    _create_agent(repo, agent_id="agent-2")
    task_1 = _create_task(repo, name="任务一")
    task_2 = _create_task(repo, name="任务二", agent_id="agent-2")

    t1_runs = [
        repo.create_scheduled_task_run(task_id=task_1.id, status="succeeded").id
        for _ in range(3)
    ]
    t2_runs = [
        repo.create_scheduled_task_run(task_id=task_2.id, status="succeeded").id
        for _ in range(2)
    ]

    grouped = repo.list_scheduled_task_runs_by_task_ids(
        task_ids=[task_1.id, task_2.id, "missing-task"],
        limit_per_task=2,
    )

    assert [r.id for r in grouped[task_1.id]] == t1_runs[-1:-3:-1]
    assert [r.id for r in grouped[task_2.id]] == t2_runs[-1:-3:-1]
    assert grouped["missing-task"] == []


def test_list_scheduled_task_runs_by_task_ids_drops_dangling_session(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo)
    task = _create_task(repo)
    run = repo.create_scheduled_task_run(task_id=task.id, status="succeeded")
    repo.update_scheduled_task_run(
        run.id,
        status="succeeded",
        session_id="ghost-session",
        error=None,
        started_at=None,
        finished_at=None,
    )

    grouped = repo.list_scheduled_task_runs_by_task_ids(
        task_ids=[task.id],
        limit_per_task=10,
    )
    assert grouped[task.id][0].session_id is None


def test_find_stale_scheduled_task_runs(repo: SqliteRepository) -> None:
    _create_agent(repo)
    task = _create_task(repo)
    now = datetime.now(UTC)

    stale = repo.create_scheduled_task_run(
        task_id=task.id,
        status="running",
        started_at=now - timedelta(hours=2),
    )
    repo.create_scheduled_task_run(
        task_id=task.id,
        status="running",
        started_at=now - timedelta(minutes=1),
    )
    repo.create_scheduled_task_run(
        task_id=task.id,
        status="succeeded",
        started_at=now - timedelta(hours=2),
    )

    found = repo.find_stale_scheduled_task_runs(
        stale_before=now - timedelta(minutes=30)
    )
    assert [r.id for r in found] == [stale.id]


def test_prune_scheduled_task_runs_keeps_newest(repo: SqliteRepository) -> None:
    _create_agent(repo)
    task = _create_task(repo)
    run_ids = [
        repo.create_scheduled_task_run(task_id=task.id, status="succeeded").id
        for _ in range(5)
    ]

    repo.prune_scheduled_task_runs(task.id, keep=3)

    remaining = [r.id for r in repo.list_scheduled_task_runs(task.id)]
    assert remaining == run_ids[-1:-4:-1]
