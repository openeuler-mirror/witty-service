from __future__ import annotations

from sqlalchemy import update

from witty_service.domain.enums import ScheduledTaskRunStatus, ScheduledTaskScheduleType
from witty_service.domain.records import ScheduledTaskRecord, ScheduledTaskRunRecord
from witty_service.persistence.entities import ScheduledTaskORM, ScheduledTaskRunORM
from witty_service.persistence.repository._base import RepositoryBase, new_id, record_of


class SchedulerRepository(RepositoryBase):
    def create_task(
        self,
        name: str,
        schedule_type: ScheduledTaskScheduleType,
        timezone: str,
        content: str,
        workspace_id: str,
        agent_id: str,
        model_id: str,
        cron_expr: str | None,
        interval_seconds: int | None,
        now: int,
    ) -> ScheduledTaskRecord:
        with self._transaction() as session:
            row = ScheduledTaskORM(
                id=new_id(),
                name=name,
                schedule_type=schedule_type.value,
                cron_expr=cron_expr,
                interval_seconds=interval_seconds,
                timezone=timezone,
                content=content,
                workspace_id=workspace_id,
                agent_id=agent_id,
                model_id=model_id,
                enabled=1,
                paused=0,
                reason=None,
                last_run_id=None,
                last_run_at=None,
                last_run_status=None,
                archived_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return record_of(ScheduledTaskRecord, row)

    def get_task(self, task_id: str) -> ScheduledTaskRecord | None:
        with self._read() as session:
            row = session.get(ScheduledTaskORM, task_id)
            return None if row is None else record_of(ScheduledTaskRecord, row)

    def list_tasks(self, workspace_id: str | None) -> list[ScheduledTaskRecord]:
        with self._read() as session:
            query = session.query(ScheduledTaskORM).filter(
                ScheduledTaskORM.archived_at.is_(None)
            )
            if workspace_id is not None:
                query = query.filter(ScheduledTaskORM.workspace_id == workspace_id)
            rows = query.order_by(ScheduledTaskORM.updated_at.desc()).all()
            return [record_of(ScheduledTaskRecord, row) for row in rows]

    def create_run(
        self,
        task_id: str,
        lease_owner: str,
        lease_expires_at: int,
        session_id: str | None,
        turn_id: str | None,
        now: int,
    ) -> ScheduledTaskRunRecord:
        with self._transaction() as session:
            row = ScheduledTaskRunORM(
                id=new_id(),
                task_id=task_id,
                session_id=session_id,
                turn_id=turn_id,
                status=ScheduledTaskRunStatus.running.value,
                lease_owner=lease_owner,
                lease_expires_at=lease_expires_at,
                error=None,
                started_at=now,
                finished_at=None,
                created_at=now,
            )
            session.add(row)
            session.flush()
            return record_of(ScheduledTaskRunRecord, row)

    def finish_run(
        self,
        run_id: str,
        status: ScheduledTaskRunStatus,
        error: str | None,
        now: int,
    ) -> ScheduledTaskRunRecord | None:
        if status == ScheduledTaskRunStatus.running:
            raise ValueError("finish_run requires a terminal status")
        with self._transaction() as session:
            result = session.execute(
                update(ScheduledTaskRunORM)
                .where(
                    ScheduledTaskRunORM.id == run_id,
                    ScheduledTaskRunORM.status == ScheduledTaskRunStatus.running.value,
                )
                .values(
                    status=status.value,
                    error=error,
                    finished_at=now,
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                return None
            row = session.get(ScheduledTaskRunORM, run_id)
            task = session.get(ScheduledTaskORM, row.task_id)
            if task is not None:
                task.last_run_id = row.id
                task.last_run_at = now
                task.last_run_status = status.value
                task.updated_at = now
            return record_of(ScheduledTaskRunRecord, row)

    def get_run(self, run_id: str) -> ScheduledTaskRunRecord | None:
        with self._read() as session:
            row = session.get(ScheduledTaskRunORM, run_id)
            return None if row is None else record_of(ScheduledTaskRunRecord, row)
