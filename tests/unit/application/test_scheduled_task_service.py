from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from witty_service.application.scheduled_task_service import ScheduledTaskService
from witty_service.config import SchedulerSettings
from witty_service.persistence.repositories import ScheduledTaskRecord


class _FakeRunRecord:
    def __init__(self, run_id: str) -> None:
        self.id = run_id


class _FakeRepository:
    def __init__(self, tasks: list[ScheduledTaskRecord] | None = None) -> None:
        self.tasks = tasks or []
        self._run_seq = 0

    def create_scheduled_task_run(
        self,
        task_id: str,
        status: str,
        started_at: datetime,
        **kwargs: object,
    ) -> _FakeRunRecord:
        self._run_seq += 1
        return _FakeRunRecord(f"run-{task_id}-{self._run_seq}")

    def list_scheduled_tasks(
        self, agent_id: str | None = None
    ) -> list[ScheduledTaskRecord]:
        if agent_id is None:
            return list(self.tasks)
        return [task for task in self.tasks if task.agent_id == agent_id]

    def find_stale_scheduled_task_runs(self, stale_before: datetime) -> list[object]:
        return []

    def update_scheduled_task_run(self, *args: object, **kwargs: object) -> None:
        return None


class _FakeScheduler:
    def __init__(self) -> None:
        self.running = False
        self.started = False
        self.job_ids: list[str] = []

    def add_job(self, fn: object, *, id: str, **kwargs: object) -> None:
        self.job_ids.append(id)

    def start(self) -> None:
        self.started = True
        self.running = True

    def get_job(self, job_id: str) -> str | None:
        return job_id if job_id in self.job_ids else None

    def remove_job(self, job_id: str) -> None:
        self.job_ids = [job for job in self.job_ids if job != job_id]


def _make_task(
    task_id: str,
    *,
    agent_id: str = "agent-1",
    schedule_type: str = "interval",
    interval_seconds: int | None = 60,
    cron_expr: str | None = None,
    enabled: bool = True,
) -> ScheduledTaskRecord:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return ScheduledTaskRecord(
        id=task_id,
        name=task_id,
        schedule_type=schedule_type,
        cron_expr=cron_expr,
        interval_seconds=interval_seconds,
        timezone="Asia/Shanghai",
        content="",
        agent_id=agent_id,
        workspace_folder=None,
        enabled=enabled,
        created_at=now,
        updated_at=now,
    )


def _make_service(
    repository: _FakeRepository | None = None,
) -> ScheduledTaskService:
    return ScheduledTaskService(
        repository=repository or _FakeRepository(),
        get_agent_manager=lambda agent_id: None,
        settings=SchedulerSettings(),
    )


@pytest.mark.asyncio
async def test_release_slot_normal_path_clears_active_run_and_busy_agent() -> None:
    service = _make_service()
    run = await service._acquire_slot(_make_task("t1"))

    assert run is not None
    await service._release_slot(task_id="t1", agent_id="agent-1", run_id=run[0])

    assert service._active_runs == {}
    assert "agent-1" not in service._busy_agents


@pytest.mark.asyncio
async def test_stale_release_after_agent_recreate_keeps_new_busy_flag() -> None:
    """P1 回归：agent 删除后同 ID 重建，旧运行释放不得清掉新运行持有的互斥标记。"""
    repository = _FakeRepository(tasks=[_make_task("t1", agent_id="agent-a")])
    service = _make_service(repository)

    old_run = await service._acquire_slot(_make_task("t1", agent_id="agent-a"))
    assert old_run is not None

    await service.handle_agent_deleted("agent-a")
    assert "agent-a" not in service._busy_agents

    # 同 ID agent 重建，新任务抢占成功。
    repository.tasks = [_make_task("t2", agent_id="agent-a")]
    new_run = await service._acquire_slot(_make_task("t2", agent_id="agent-a"))
    assert new_run is not None

    # 旧运行的 finally 走到迟到的 release：不得清掉新运行持有的 busy 标记。
    await service._release_slot(task_id="t1", agent_id="agent-a", run_id=old_run[0])

    assert service._active_runs == {"t2": new_run[0]}
    assert "agent-a" in service._busy_agents

    # 新运行正常结束后互斥状态最终被清空。
    await service._release_slot(task_id="t2", agent_id="agent-a", run_id=new_run[0])
    assert service._active_runs == {}
    assert "agent-a" not in service._busy_agents


@pytest.mark.asyncio
async def test_handle_agent_deleted_cleans_state_and_cancels_inflight() -> None:
    repository = _FakeRepository(tasks=[_make_task("t1", agent_id="agent-a")])
    service = _make_service(repository)
    run = await service._acquire_slot(_make_task("t1", agent_id="agent-a"))
    assert run is not None

    async def _never() -> None:
        await asyncio.sleep(3600)

    bg_task = asyncio.ensure_future(_never())
    service._background_tasks.add(bg_task)
    service._bg_tasks_by_task["t1"] = {bg_task}

    await service.handle_agent_deleted("agent-a")
    await asyncio.sleep(0)

    assert bg_task.cancelled()
    assert service._active_runs == {}
    assert "agent-a" not in service._busy_agents
    assert "t1" not in service._bg_tasks_by_task
    bg_task.cancel()


@pytest.mark.asyncio
async def test_start_skips_invalid_task_and_still_starts_scheduler() -> None:
    """P2b 回归：单个坏任务不得中断启动循环，其余任务照常注册。"""
    repository = _FakeRepository(
        tasks=[
            _make_task(
                "bad-task",
                schedule_type="cron",
                cron_expr=None,
                interval_seconds=None,
            ),
            _make_task("good-task"),
        ]
    )
    service = _make_service(repository)
    service._scheduler = _FakeScheduler()  # type: ignore[assignment]

    await service.start()

    assert service._scheduler.started
    assert service._scheduler.job_ids == ["good-task"]
