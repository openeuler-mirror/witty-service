"""定时任务统一调度服务。

通过 APScheduler 统一调度所有 runtime（openclaw/opencode）：到期时确保 agent
可用并新建会话，经 ``AgentManager.send_message`` 执行，结果以会话/消息/运行
记录留存；手动触发复用前端已建会话，消息经既有 SSE 管线实时回传。

当前仅支持单进程部署：互斥（``_active_runs``/``_busy_agents``）只存在于本进程
内存，横向扩展需将互斥下沉到数据库或分布式锁。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore
from apscheduler.triggers.cron import CronTrigger  # type: ignore
from apscheduler.triggers.interval import IntervalTrigger  # type: ignore

from witty_service.config import SchedulerSettings, get_settings
from witty_service.domain.enums import AgentStatus, ScheduledTaskRunStatus
from witty_service.domain.errors import DomainError
from witty_service.persistence.orm import MessageStatus
from witty_service.persistence.repositories import (
    AgentRecord,
    ScheduledTaskRecord,
    ScheduledTaskRunRecord,
    ScheduledTaskRunWithTaskRecord,
    ScheduledTaskSessionRecord,
    SqliteRepository,
)

logger = logging.getLogger(__name__)

TASK_NOT_FOUND = "TASK_NOT_FOUND"
TASK_RUN_NOT_FOUND = "TASK_RUN_NOT_FOUND"
TASK_AGENT_NOT_FOUND = "TASK_AGENT_NOT_FOUND"
TASK_AGENT_NOT_RUNNABLE = "TASK_AGENT_NOT_RUNNABLE"
TASK_BUSY = "TASK_BUSY"
TASK_INVALID_SCHEDULE = "TASK_INVALID_SCHEDULE"
TASK_INVALID_WORKSPACE_FOLDER = "TASK_INVALID_WORKSPACE_FOLDER"
TASK_SESSION_NOT_FOUND = "TASK_SESSION_NOT_FOUND"

_UNSET = object()


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ScheduledTaskTemplate:
    """内置定时任务模板，仅作新建参考。"""

    key: str
    name: str
    description: str
    schedule_type: str
    cron_expr: str | None
    interval_seconds: int | None
    timezone: str
    content: str
    workspace_folder: str | None


@dataclass(frozen=True)
class TaskListOverview:
    """列表聚合查询结果：任务 + 最近 N 条 run + 会话摘要 + 在飞任务集合。"""

    tasks: list[ScheduledTaskRecord]
    runs_by_task: dict[str, list[ScheduledTaskRunRecord]]
    sessions_by_task: dict[str, list[ScheduledTaskSessionRecord]]
    running_task_ids: set[str]


BUILTIN_TEMPLATES: tuple[ScheduledTaskTemplate, ...] = (
    ScheduledTaskTemplate(
        key="daily_competitor_tracking",
        name="每日竞品动态追踪",
        description="每天定时抓取并汇总竞品动态、文档更新与版本发布信息。",
        schedule_type="cron",
        cron_expr="0 9 * * *",
        interval_seconds=None,
        timezone="Asia/Shanghai",
        content=(
            "请执行每日竞品动态追踪：收集主要竞品的最新动态（新闻、文档更新、"
            "版本发布、社交媒体公告），整理成带来源链接的结构化摘要，并输出到"
            "工作目录下的今日报告文件中。"
        ),
        workspace_folder="code/competitor-tracking",
    ),
)


AgentManagerFactory = Callable[[str], Any]


class ScheduledTaskService:
    """定时任务调度服务：DB 是唯一事实源，APScheduler 负责到期触发。"""

    def __init__(
        self,
        *,
        repository: SqliteRepository,
        get_agent_manager: AgentManagerFactory,
        settings: SchedulerSettings | None = None,
    ) -> None:
        self._repository = repository
        self._get_agent_manager = get_agent_manager
        self._settings = settings or get_settings().scheduler
        self._scheduler = AsyncIOScheduler(timezone=ZoneInfo(self._settings.timezone))
        self._state_lock = asyncio.Lock()
        self._active_runs: dict[str, str] = {}
        self._busy_agents: set[str] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        # task_id -> 该任务在飞的 asyncio.Task，用于定位/取消指定任务的运行。
        self._bg_tasks_by_task: dict[str, set[asyncio.Task[Any]]] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动调度器：恢复孤儿运行记录并注册全部 enabled 任务。"""
        if self._scheduler.running:
            return
        self._recover_orphan_runs()
        tasks = self._repository.list_scheduled_tasks()
        enabled_count = sum(1 for task in tasks if task.enabled)
        skipped = 0
        for task in tasks:
            if not task.enabled:
                continue
            try:
                self._schedule_task(task)
            except Exception:
                skipped += 1
                logger.exception(
                    "Skipped scheduling task %s due to invalid schedule configuration.",
                    task.id,
                )
        self._scheduler.start()
        logger.info(
            "Scheduled task scheduler started with %d enabled task(s)", enabled_count
        )
        if skipped:
            logger.warning(
                "%d enabled task(s) skipped due to invalid schedule configuration",
                skipped,
            )

    async def shutdown(self) -> None:
        # 先取消在飞的运行任务，避免 run 记录遗留为 running，只能靠孤儿恢复兜底。
        if self._background_tasks:
            for bg_task in list(self._background_tasks):
                bg_task.cancel()
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduled task scheduler stopped")

    def _recover_orphan_runs(self) -> None:
        stale_before = utcnow() - timedelta(
            seconds=self._settings.orphan_run_timeout_seconds
        )
        recovered = 0
        for run in self._repository.find_stale_scheduled_task_runs(
            stale_before=stale_before
        ):
            try:
                self._repository.update_scheduled_task_run(
                    run.id,
                    status=ScheduledTaskRunStatus.failed,
                    session_id=run.session_id,
                    error="Run interrupted by service restart.",
                    started_at=run.started_at,
                    finished_at=utcnow(),
                )
                recovered += 1
            except Exception:
                logger.exception(
                    "Failed to recover stale scheduled task run: %s", run.id
                )
        if recovered:
            logger.info("Recovered %d stale scheduled task run(s)", recovered)

    # ------------------------------------------------------------------
    # 模板
    # ------------------------------------------------------------------

    def list_templates(self) -> list[ScheduledTaskTemplate]:
        return list(BUILTIN_TEMPLATES)

    # ------------------------------------------------------------------
    # 任务 CRUD 与启停
    # ------------------------------------------------------------------

    def create_task(
        self,
        *,
        name: str,
        schedule_type: str,
        cron_expr: str | None,
        interval_seconds: int | None,
        timezone_name: str,
        content: str,
        agent_id: str,
        workspace_folder: str | None = None,
        enabled: bool = True,
    ) -> ScheduledTaskRecord:
        agent = self._repository.get_agent(agent_id)
        if agent is None or agent.status is AgentStatus.deleted:
            raise DomainError(
                code=TASK_AGENT_NOT_FOUND,
                message="Agent was not found.",
                details={"agent_id": agent_id},
                status_code=404,
            )
        self._validate_schedule(
            schedule_type=schedule_type,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            timezone_name=timezone_name,
        )
        self._validate_workspace_folder(agent=agent, workspace_folder=workspace_folder)
        task = self._repository.create_scheduled_task(
            name=name,
            schedule_type=schedule_type,
            cron_expr=cron_expr,
            interval_seconds=interval_seconds,
            timezone=timezone_name,
            content=content,
            agent_id=agent_id,
            workspace_folder=workspace_folder,
            enabled=enabled,
        )
        if task.enabled:
            self._schedule_task(task)
        return task

    def get_task(self, task_id: str) -> ScheduledTaskRecord:
        return self._require_task(task_id)

    def list_tasks(self, agent_id: str | None = None) -> list[ScheduledTaskRecord]:
        return self._repository.list_scheduled_tasks(agent_id=agent_id)

    def list_tasks_overview(
        self,
        agent_id: str | None = None,
        include_runs: int | None = None,
        sessions_per_task: int = 50,
    ) -> TaskListOverview:
        """列表聚合查询：任务 + 最近 N 条 run + 各任务执行会话摘要 + 在飞任务集合。

        - runs_by_task：include_runs 为空时为空字典（列表接口 recent_runs 为空列表）；
        - sessions_by_task：侧栏会话中心化渲染的数据源（含关联 run 状态注脚）；
        - running_task_ids：存在 running run 的任务 id，用于 has_running_run。
        """
        tasks = self.list_tasks(agent_id=agent_id)
        task_ids = [task.id for task in tasks]
        runs_by_task: dict[str, list[ScheduledTaskRunRecord]] = {}
        if include_runs is not None:
            runs_by_task = self._repository.list_scheduled_task_runs_by_task_ids(
                task_ids=task_ids,
                limit_per_task=include_runs,
            )
        sessions_by_task = self._repository.list_scheduled_task_sessions_by_task_ids(
            task_ids=task_ids,
            limit_per_task=sessions_per_task,
        )
        running_task_ids = self._repository.list_task_ids_with_running_runs(task_ids)
        return TaskListOverview(
            tasks=tasks,
            runs_by_task=runs_by_task,
            sessions_by_task=sessions_by_task,
            running_task_ids=running_task_ids,
        )

    def list_task_runs(
        self, task_id: str, limit: int = 100
    ) -> list[ScheduledTaskRunRecord]:
        self._require_task(task_id)
        return self._repository.list_scheduled_task_runs(task_id=task_id, limit=limit)

    def list_runs_page(
        self,
        limit: int = 20,
        offset: int = 0,
        agent_id: str | None = None,
    ) -> tuple[list[ScheduledTaskRunWithTaskRecord], int]:
        """跨任务聚合分页查询执行记录，供执行记录页使用（含任务名/agent）。"""
        return self._repository.list_scheduled_task_runs_page(
            limit=limit,
            offset=offset,
            agent_id=agent_id,
        )

    def get_task_run(self, task_id: str, run_id: str) -> ScheduledTaskRunRecord:
        run = self._repository.get_scheduled_task_run(run_id)
        if run is None or run.task_id != task_id:
            raise DomainError(
                code=TASK_RUN_NOT_FOUND,
                message="Scheduled task run was not found.",
                details={"task_id": task_id, "run_id": run_id},
                status_code=404,
            )
        return run

    def update_task(
        self,
        task_id: str,
        *,
        name: Any = _UNSET,
        schedule_type: Any = _UNSET,
        cron_expr: Any = _UNSET,
        interval_seconds: Any = _UNSET,
        timezone_name: Any = _UNSET,
        content: Any = _UNSET,
        workspace_folder: Any = _UNSET,
    ) -> ScheduledTaskRecord:
        current = self._require_task(task_id)

        merged_name = current.name if name is _UNSET else name
        merged_schedule_type = (
            current.schedule_type if schedule_type is _UNSET else schedule_type
        )
        merged_timezone = current.timezone if timezone_name is _UNSET else timezone_name
        merged_content = current.content if content is _UNSET else content
        merged_folder = (
            current.workspace_folder if workspace_folder is _UNSET else workspace_folder
        )
        if merged_schedule_type == "cron":
            merged_cron_expr = current.cron_expr if cron_expr is _UNSET else cron_expr
            merged_interval_seconds = None
        else:
            merged_cron_expr = None
            merged_interval_seconds = (
                current.interval_seconds
                if interval_seconds is _UNSET
                else interval_seconds
            )

        self._validate_schedule(
            schedule_type=merged_schedule_type,
            cron_expr=merged_cron_expr,
            interval_seconds=merged_interval_seconds,
            timezone_name=merged_timezone,
        )
        # 与 create_task 一致：agent 已删除时同样拒绝，避免任务被静默重新调度。
        agent = self._repository.get_agent(current.agent_id)
        if agent is None or agent.status is AgentStatus.deleted:
            raise DomainError(
                code=TASK_AGENT_NOT_FOUND,
                message="Agent was not found.",
                details={"agent_id": current.agent_id},
                status_code=404,
            )
        if merged_folder:
            self._validate_workspace_folder(agent=agent, workspace_folder=merged_folder)
        updated = self._repository.update_scheduled_task(
            task_id,
            name=merged_name,
            schedule_type=merged_schedule_type,
            cron_expr=merged_cron_expr,
            interval_seconds=merged_interval_seconds,
            timezone=merged_timezone,
            content=merged_content,
            workspace_folder=merged_folder,
            enabled=current.enabled,
        )
        if updated.enabled:
            self._schedule_task(updated)
        else:
            self._unschedule_task(updated.id)
        return updated

    def delete_task(self, task_id: str) -> None:
        task = self._repository.get_scheduled_task(task_id)
        if task is None:
            return
        # 有在飞运行的任务禁止删除：级联删除会丢运行记录，返回 409 由客户端重试。
        if task_id in self._active_runs:
            raise DomainError(
                code=TASK_BUSY,
                message="Task has an in-flight run and cannot be deleted.",
                details={"task_id": task_id},
                status_code=409,
            )
        self._unschedule_task(task_id)
        self._repository.delete_scheduled_task(task_id)

    async def handle_agent_deleted(self, agent_id: str) -> None:
        """Agent 删除前的调度器清理：移除该 agent 所有任务的 job 与互斥状态。

        必须在数据库级联删除任务前调用。持有 _state_lock，与
        _acquire_slot/_release_slot 的互斥状态修改保持一致的锁协议。
        """
        async with self._state_lock:
            task_ids = [
                task.id
                for task in self._repository.list_scheduled_tasks(agent_id=agent_id)
            ]
            for task_id in task_ids:
                self._unschedule_task(task_id)
                self._active_runs.pop(task_id, None)
                # 取消该任务在飞的运行，避免其继续对着已删除的 agent 空转。
                for bg_task in self._bg_tasks_by_task.pop(task_id, set()):
                    bg_task.cancel()
            self._busy_agents.discard(agent_id)

    def enable_task(self, task_id: str) -> ScheduledTaskRecord:
        self._require_task(task_id)
        updated = self._repository.set_scheduled_task_enabled(task_id, True)
        self._schedule_task(updated)
        return updated

    def disable_task(self, task_id: str) -> ScheduledTaskRecord:
        self._require_task(task_id)
        updated = self._repository.set_scheduled_task_enabled(task_id, False)
        self._unschedule_task(task_id)
        return updated

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------

    async def run_task_now(
        self,
        task_id: str,
        session_id: str | None = None,
    ) -> ScheduledTaskRunRecord:
        """手动触发任务（不受 enabled 状态限制），立即返回登记的运行记录。

        前端已建会话时校验归属后复用，否则由后端新建会话。
        """
        task = self._require_task(task_id)
        return await self._dispatch_run(task, session_id=session_id)

    async def _execute_task(self, task_id: str) -> ScheduledTaskRunRecord | None:
        """调度器 job 回调：enabled 任务到点触发，忙碌则登记 skipped。"""
        task = self._repository.get_scheduled_task(task_id)
        if task is None or not task.enabled:
            return None
        return await self._dispatch_run(task)

    async def _dispatch_run(
        self,
        task: ScheduledTaskRecord,
        *,
        session_id: str | None = None,
    ) -> ScheduledTaskRunRecord:
        """抢占互斥槽并后台派发一次运行；忙碌时登记 skipped 记录。

        前端已建会话时，必须在返回运行记录前同步完成会话归属标记与
        session_id 回填：否则页面刷新可能落在“会话已创建、后端尚未打标”
        的窗口，把定时执行会话误归入普通会话。
        """
        acquired = await self._acquire_slot(task)
        if acquired is None:
            return self._record_skipped(task.id)
        run_id, started_at = acquired

        prepared_session_id = session_id
        if prepared_session_id is not None:
            try:
                prepared_session_id = self._validate_provided_session(
                    task, prepared_session_id
                )
                self._repository.mark_session_scheduled(prepared_session_id, task.id)
                self._repository.update_scheduled_task_run(
                    run_id,
                    status=ScheduledTaskRunStatus.running,
                    session_id=prepared_session_id,
                    error=None,
                    started_at=started_at,
                    finished_at=None,
                )
            except Exception as exc:
                await self._release_slot(
                    task_id=task.id,
                    agent_id=task.agent_id,
                    run_id=run_id,
                )
                self._repository.update_scheduled_task_run(
                    run_id,
                    status=ScheduledTaskRunStatus.failed,
                    session_id=prepared_session_id,
                    error=f"{type(exc).__name__}: {exc}",
                    started_at=started_at,
                    finished_at=utcnow(),
                )
                raise

        self._spawn_run(
            task=task,
            run_id=run_id,
            started_at=started_at,
            session_id=prepared_session_id,
        )
        return self._require_run(run_id)

    def _spawn_run(
        self,
        *,
        task: ScheduledTaskRecord,
        run_id: str,
        started_at: datetime,
        session_id: str | None = None,
    ) -> None:
        """在后台异步执行一次运行，调度器 job 与手动触发均立即返回。"""
        bg_task = asyncio.get_running_loop().create_task(
            self._run_in_flight(
                task=task,
                run_id=run_id,
                started_at=started_at,
                provided_session_id=session_id,
            )
        )
        # 保存强引用，避免后台任务在运行期间被 GC 回收（asyncio 官方建议）。
        self._background_tasks.add(bg_task)
        self._bg_tasks_by_task.setdefault(task.id, set()).add(bg_task)

        def _done(_bg: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(_bg)
            by_task = self._bg_tasks_by_task.get(task.id)
            if by_task is not None:
                by_task.discard(_bg)
                if not by_task:
                    self._bg_tasks_by_task.pop(task.id, None)

        bg_task.add_done_callback(_done)

    async def _run_in_flight(
        self,
        *,
        task: ScheduledTaskRecord,
        run_id: str,
        started_at: datetime,
        provided_session_id: str | None = None,
    ) -> None:
        session_id: str | None = None
        status: ScheduledTaskRunStatus = ScheduledTaskRunStatus.succeeded
        error: str | None = None
        try:
            session_id = await self._run_agent_turn(
                task,
                run_id=run_id,
                started_at=started_at,
                provided_session_id=provided_session_id,
            )
        except asyncio.CancelledError:
            status = ScheduledTaskRunStatus.failed
            error = "Run cancelled during service shutdown."
            raise
        except Exception as exc:
            status = ScheduledTaskRunStatus.failed
            error = (
                f"{exc.code}: {exc}"
                if isinstance(exc, DomainError)
                else f"{type(exc).__name__}: {exc}"
            )
            logger.exception(
                "Scheduled task run failed: task_id=%s run_id=%s",
                task.id,
                run_id,
            )
            # 失败若发生在会话创建之后，从运行记录读回已回填的 session_id。
            try:
                current_run = self._repository.get_scheduled_task_run(run_id)
            except Exception:
                current_run = None
            if current_run is not None and current_run.session_id:
                session_id = current_run.session_id
        finally:
            await self._release_slot(
                task_id=task.id,
                agent_id=task.agent_id,
                run_id=run_id,
            )
            try:
                self._repository.update_scheduled_task_run(
                    run_id,
                    status=status,
                    session_id=session_id,
                    error=error,
                    started_at=started_at,
                    finished_at=utcnow(),
                )
            except Exception:
                logger.warning(
                    "Failed to persist scheduled task run result: task_id=%s run_id=%s",
                    task.id,
                    run_id,
                    exc_info=True,
                )
            self._prune_runs(task.id)

    async def _acquire_slot(
        self, task: ScheduledTaskRecord
    ) -> tuple[str, datetime] | None:
        """任务级/agent 级互斥：占用成功返回 (run_id, started_at)，否则返回 None。"""
        async with self._state_lock:
            if task.id in self._active_runs:
                return None
            if task.agent_id in self._busy_agents:
                return None
            started_at = utcnow()
            run = self._repository.create_scheduled_task_run(
                task_id=task.id,
                status=ScheduledTaskRunStatus.running,
                started_at=started_at,
            )
            self._active_runs[task.id] = run.id
            self._busy_agents.add(task.agent_id)
            return run.id, started_at

    async def _release_slot(
        self,
        *,
        task_id: str,
        agent_id: str,
        run_id: str,
    ) -> None:
        async with self._state_lock:
            if self._active_runs.get(task_id) == run_id:
                self._active_runs.pop(task_id, None)
                self._busy_agents.discard(agent_id)

    def _record_skipped(self, task_id: str) -> ScheduledTaskRunRecord:
        run = self._repository.create_scheduled_task_run(
            task_id=task_id,
            status=ScheduledTaskRunStatus.skipped,
            error="Task or agent is busy, this run was skipped.",
            finished_at=utcnow(),
        )
        # 跳过记录同样受上限约束，避免任务/agent 长时间忙碌时无限堆积。
        self._prune_runs(task_id)
        return run

    async def _run_agent_turn(
        self,
        task: ScheduledTaskRecord,
        *,
        run_id: str,
        started_at: datetime,
        provided_session_id: str | None = None,
    ) -> str:
        agent = self._repository.get_agent(task.agent_id)
        if agent is None or agent.status is AgentStatus.deleted:
            raise DomainError(
                code=TASK_AGENT_NOT_FOUND,
                message="Agent was not found.",
                details={"agent_id": task.agent_id},
                status_code=404,
            )

        manager = self._get_agent_manager(task.agent_id)
        if agent.status is AgentStatus.paused:
            agent = await manager.resume_agent(task.agent_id)
        if agent.status is not AgentStatus.running:
            raise DomainError(
                code=TASK_AGENT_NOT_RUNNABLE,
                message="Agent must be running or paused to execute a scheduled task.",
                details={
                    "agent_id": task.agent_id,
                    "status": agent.status.value,
                },
                status_code=409,
            )

        self._prepare_workspace_folder(task)
        if provided_session_id is not None:
            # 手动触发：复用前端已建会话，跳过建会话阶段，消息流直接进既有 SSE 管线。
            session_id = self._validate_provided_session(task, provided_session_id)
        else:
            session = await manager.create_session(task.agent_id)
            session_id = str(session.id)

        # 记录会话归属：任务删除时按该标记级联清理该任务的全部执行会话。
        try:
            self._repository.mark_session_scheduled(session_id, task.id)
        except Exception:
            logger.warning(
                "Failed to mark session as scheduled: task_id=%s session_id=%s",
                task.id,
                session_id,
                exc_info=True,
            )

        # 会话创建后立即回填运行记录，前端轮询可及时发现会话并接入流式渲染。
        try:
            self._repository.update_scheduled_task_run(
                run_id,
                status=ScheduledTaskRunStatus.running,
                session_id=session_id,
                error=None,
                started_at=started_at,
                finished_at=None,
            )
        except Exception:
            logger.warning(
                "Failed to persist scheduled task run session id: task_id=%s run_id=%s",
                task.id,
                run_id,
                exc_info=True,
            )

        # 复用正常对话的流式管线：事件经 SessionStreamRegistry 广播给前端。
        completed = False
        last_event_type: str | None = None
        error_payload: dict[str, Any] | None = None
        async for chunk in manager.send_message_stream(
            task.agent_id, session_id, self._build_message(task)
        ):
            event = chunk.get("event") if isinstance(chunk, dict) else None
            if not isinstance(event, dict):
                continue
            last_event_type = event.get("type")
            if last_event_type in {"message.completed", "turn.completed"}:
                completed = True
            elif last_event_type in {"stream.error", "client.error"}:
                payload = event.get("payload")
                error_payload = payload if isinstance(payload, dict) else None

        if not completed:
            if self._is_session_aborted(session_id):
                raise DomainError(
                    code="TASK_RUN_ABORTED",
                    message="Run aborted by user.",
                    details={"session_id": session_id},
                )
            if error_payload:
                raise DomainError(
                    code=str(error_payload.get("code") or "STREAM_ERROR"),
                    message=str(
                        error_payload.get("message") or "Scheduled task stream failed."
                    ),
                    details={"session_id": session_id},
                )
            raise DomainError(
                code="INVALID_MESSAGE_STREAM",
                message="Message stream terminated before completion event.",
                details={
                    "agent_id": task.agent_id,
                    "session_id": session_id,
                    "last_event_type": last_event_type,
                },
            )
        return session_id

    def _is_session_aborted(self, session_id: str) -> bool:
        """判断会话是否已被用户中止（abort 端点会把最后一条助手消息标记为 interrupted）。"""
        last_msg = self._repository.find_last_assistant_message_for_session(session_id)
        return last_msg is not None and last_msg.status is MessageStatus.interrupted

    def _validate_provided_session(
        self,
        task: ScheduledTaskRecord,
        session_id: str,
    ) -> str:
        """校验前端传入的会话存在且归属于任务 agent，防止串用其他 agent 的会话。"""
        session = self._repository.get_session(session_id)
        if session is None or session.agent_id != task.agent_id:
            raise DomainError(
                code=TASK_SESSION_NOT_FOUND,
                message="Session was not found for the task agent.",
                details={
                    "task_id": task.id,
                    "agent_id": task.agent_id,
                    "session_id": session_id,
                },
                status_code=404,
            )
        return session_id

    def _prepare_workspace_folder(self, task: ScheduledTaskRecord) -> Path | None:
        if not task.workspace_folder:
            return None
        agent = self._repository.get_agent(task.agent_id)
        if agent is None:
            raise DomainError(
                code=TASK_AGENT_NOT_FOUND,
                message="Agent was not found.",
                details={"agent_id": task.agent_id},
                status_code=404,
            )
        self._validate_workspace_folder(
            agent=agent, workspace_folder=task.workspace_folder
        )
        base = Path(agent.workspace_path).expanduser().resolve()
        target = (base / task.workspace_folder).resolve()
        target.mkdir(parents=True, exist_ok=True)
        return target

    def _validate_workspace_folder(
        self,
        *,
        agent: AgentRecord,
        workspace_folder: str | None,
    ) -> None:
        """校验 workspace_folder 不逃逸 agent 工作区（创建/更新时 fail-fast，运行前兜底）。"""
        if not workspace_folder:
            return
        base = Path(agent.workspace_path).expanduser().resolve()
        target = (base / workspace_folder).resolve()
        if not target.is_relative_to(base):
            raise DomainError(
                code=TASK_INVALID_WORKSPACE_FOLDER,
                message="Workspace folder must be inside the agent workspace.",
                details={
                    "agent_id": agent.id,
                    "workspace_folder": workspace_folder,
                },
            )

    @staticmethod
    def _build_message(task: ScheduledTaskRecord) -> str:
        message = task.content.strip()
        if task.workspace_folder:
            message += (
                "\n\n工作目录："
                + task.workspace_folder
                + "（相对当前 agent 工作区；请在指定目录下完成工作内容）"
            )
        return message

    def _prune_runs(self, task_id: str) -> None:
        try:
            self._repository.prune_scheduled_task_runs(
                task_id, self._settings.max_run_records
            )
        except Exception:
            logger.exception("Failed to prune scheduled task runs: task_id=%s", task_id)

    # ------------------------------------------------------------------
    # 调度器内部操作
    # ------------------------------------------------------------------

    def _schedule_task(self, task: ScheduledTaskRecord) -> None:
        trigger = self._build_trigger(task)
        self._scheduler.add_job(
            self._execute_task,
            trigger=trigger,
            id=task.id,
            args=[task.id],
            replace_existing=True,
            misfire_grace_time=self._settings.misfire_grace_seconds,
            coalesce=True,
        )

    def _unschedule_task(self, task_id: str) -> None:
        if self._scheduler.get_job(task_id) is not None:
            self._scheduler.remove_job(task_id)

    def _build_trigger(self, task: ScheduledTaskRecord) -> Any:
        if task.schedule_type == "cron":
            if not task.cron_expr:
                raise DomainError(
                    code=TASK_INVALID_SCHEDULE,
                    message="cron_expr is required for cron schedules.",
                    details={"task_id": task.id},
                )
            try:
                return CronTrigger.from_crontab(
                    task.cron_expr, timezone=ZoneInfo(task.timezone)
                )
            except (ValueError, ZoneInfoNotFoundError) as exc:
                raise DomainError(
                    code=TASK_INVALID_SCHEDULE,
                    message="Invalid cron expression or timezone.",
                    details={
                        "cron_expr": task.cron_expr,
                        "timezone": task.timezone,
                    },
                ) from exc
        if task.schedule_type == "interval":
            if not task.interval_seconds or task.interval_seconds <= 0:
                raise DomainError(
                    code=TASK_INVALID_SCHEDULE,
                    message="interval_seconds must be a positive integer.",
                    details={"interval_seconds": task.interval_seconds},
                )
            return IntervalTrigger(seconds=task.interval_seconds)
        raise DomainError(
            code=TASK_INVALID_SCHEDULE,
            message="schedule_type must be 'cron' or 'interval'.",
            details={"schedule_type": task.schedule_type},
        )

    def _validate_schedule(
        self,
        *,
        schedule_type: str,
        cron_expr: str | None,
        interval_seconds: int | None,
        timezone_name: str,
    ) -> None:
        try:
            ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise DomainError(
                code=TASK_INVALID_SCHEDULE,
                message="Invalid timezone.",
                details={"timezone": timezone_name},
            ) from exc
        if schedule_type == "cron":
            if not cron_expr:
                raise DomainError(
                    code=TASK_INVALID_SCHEDULE,
                    message="cron_expr is required when schedule_type is 'cron'.",
                )
            try:
                CronTrigger.from_crontab(cron_expr, timezone=ZoneInfo(timezone_name))
            except ValueError as exc:
                raise DomainError(
                    code=TASK_INVALID_SCHEDULE,
                    message="Invalid cron expression.",
                    details={"cron_expr": cron_expr},
                ) from exc
        elif schedule_type == "interval":
            if not interval_seconds or interval_seconds <= 0:
                raise DomainError(
                    code=TASK_INVALID_SCHEDULE,
                    message="interval_seconds must be a positive integer.",
                    details={"interval_seconds": interval_seconds},
                )
        else:
            raise DomainError(
                code=TASK_INVALID_SCHEDULE,
                message="schedule_type must be 'cron' or 'interval'.",
                details={"schedule_type": schedule_type},
            )

    def _require_task(self, task_id: str) -> ScheduledTaskRecord:
        task = self._repository.get_scheduled_task(task_id)
        if task is None:
            raise DomainError(
                code=TASK_NOT_FOUND,
                message="Scheduled task was not found.",
                details={"task_id": task_id},
                status_code=404,
            )
        return task

    def _require_run(self, run_id: str) -> ScheduledTaskRunRecord:
        run = self._repository.get_scheduled_task_run(run_id)
        if run is None:
            raise DomainError(
                code=TASK_RUN_NOT_FOUND,
                message="Scheduled task run was not found.",
                details={"run_id": run_id},
                status_code=404,
            )
        return run
