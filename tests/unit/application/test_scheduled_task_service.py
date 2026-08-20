from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from witty_service.application.scheduled_task_service import ScheduledTaskService
from witty_service.config import SchedulerSettings
from witty_service.domain.enums import AgentStatus, ScheduledTaskRunStatus
from witty_service.domain.errors import DomainError
from witty_service.persistence.orm import MessageStatus
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

    def list_scheduled_task_runs_page(
        self,
        limit: int = 20,
        offset: int = 0,
        agent_id: str | None = None,
    ) -> tuple[list[object], int]:
        return [], 0


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


def test_list_runs_page_delegates_to_repository() -> None:
    service = _make_service()

    records, total = service.list_runs_page(limit=10, offset=5, agent_id="agent-1")

    assert records == []
    assert total == 0


class _CancellationFakeRepository(_FakeRepository):
    def __init__(self) -> None:
        super().__init__()
        self.run_updates: list[dict[str, object]] = []

    def update_scheduled_task_run(
        self, *args: object, **kwargs: object
    ) -> _FakeRunRecord:
        self.run_updates.append(kwargs)
        return _FakeRunRecord(str(kwargs.get("run_id", "run-1")))

    def prune_scheduled_task_runs(self, task_id: str, keep: int) -> None:
        return None


@pytest.mark.asyncio
async def test_run_in_flight_marks_cancelled_run_as_failed() -> None:
    """服务关闭/任务取消时，运行记录不得被初始的 succeeded 状态覆盖。"""
    repository = _CancellationFakeRepository()
    service = _make_service(repository)
    task = _make_task("t1")

    async def _raise_cancel(
        task: ScheduledTaskRecord,
        *,
        run_id: str,
        started_at: datetime,
        provided_session_id: str | None = None,
    ) -> str:
        raise asyncio.CancelledError

    service._run_agent_turn = _raise_cancel  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await service._run_in_flight(
            task=task,
            run_id="run-1",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    assert repository.run_updates[-1]["status"] == ScheduledTaskRunStatus.failed
    assert (
        repository.run_updates[-1]["error"] == "Run cancelled during service shutdown."
    )


class _SyncMarkFakeRepository(_FakeRepository):
    def __init__(self) -> None:
        super().__init__()
        self.marked_sessions: list[tuple[str, str]] = []
        self.run_updates: list[dict[str, object]] = []

    def get_session(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=session_id, agent_id="agent-1")

    def mark_session_scheduled(self, session_id: str, task_id: str) -> None:
        self.marked_sessions.append((session_id, task_id))

    def update_scheduled_task_run(self, *args: object, **kwargs: object) -> None:
        self.run_updates.append(kwargs)


@pytest.mark.asyncio
async def test_dispatch_run_marks_provided_session_before_return() -> None:
    """手动触发复用前端会话时，必须在返回 run 前完成 scheduled_task_id 打标。"""
    repository = _SyncMarkFakeRepository()
    service = _make_service(repository)
    service._spawn_run = lambda **kwargs: None  # type: ignore[method-assign]
    service._require_run = lambda run_id: _FakeRunRecord(run_id)  # type: ignore[method-assign]

    await service._dispatch_run(_make_task("t1"), session_id="session-1")

    assert repository.marked_sessions == [("session-1", "t1")]
    assert any(
        update.get("session_id") == "session-1"
        and update.get("status") == ScheduledTaskRunStatus.running
        for update in repository.run_updates
    )


class _AbortedRunFakeRepository(_FakeRepository):
    def get_agent(self, agent_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=agent_id, status=AgentStatus.running)

    def get_session(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(id=session_id, agent_id="agent-1")

    def mark_session_scheduled(self, session_id: str, task_id: str) -> None:
        return None

    def update_scheduled_task_run(self, *args: object, **kwargs: object) -> None:
        return None

    def find_last_assistant_message_for_session(
        self, session_id: str
    ) -> SimpleNamespace:
        return SimpleNamespace(status=MessageStatus.interrupted)


class _EndsWithoutTerminalManager:
    async def send_message_stream(self, agent_id: str, session_id: str, content: str):
        if False:  # pragma: no cover - keeps this an async generator
            yield {}


@pytest.mark.asyncio
async def test_run_agent_turn_recognizes_user_abort() -> None:
    """会话被用户 abort 后，流未正常完成时应抛 TASK_RUN_ABORTED。"""
    service = _make_service(_AbortedRunFakeRepository())
    service._get_agent_manager = lambda agent_id: _EndsWithoutTerminalManager()  # type: ignore[method-assign]
    service._prepare_workspace_folder = lambda task: None  # type: ignore[method-assign]

    with pytest.raises(DomainError) as exc_info:
        await service._run_agent_turn(
            _make_task("t1"),
            run_id="run-1",
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            provided_session_id="session-1",
        )

    assert exc_info.value.code == "TASK_RUN_ABORTED"
