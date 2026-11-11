from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from witty_service.persistence.entities.base import Base, utc_ms_now


class ScheduledTaskORM(Base):
    __tablename__ = "scheduled_tasks"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_scheduled_tasks"),
        CheckConstraint(
            "schedule_type IN ('cron', 'interval')",
            name="ck_scheduled_tasks_schedule_type",
        ),
        CheckConstraint(
            "(schedule_type = 'cron' AND cron_expr IS NOT NULL AND "
            "interval_seconds IS NULL) OR "
            "(schedule_type = 'interval' AND interval_seconds IS NOT NULL AND "
            "cron_expr IS NULL)",
            name="ck_scheduled_tasks_schedule",
        ),
        CheckConstraint(
            "interval_seconds IS NULL OR interval_seconds > 0",
            name="ck_scheduled_tasks_interval_seconds",
        ),
        CheckConstraint("enabled IN (0, 1)", name="ck_scheduled_tasks_enabled"),
        CheckConstraint("paused IN (0, 1)", name="ck_scheduled_tasks_paused"),
        CheckConstraint(
            "(paused = 0 AND reason IS NULL) OR (paused = 1 AND reason IS NOT NULL)",
            name="ck_scheduled_tasks_pause",
        ),
        CheckConstraint(
            "(last_run_id IS NULL AND last_run_at IS NULL AND "
            "last_run_status IS NULL) OR "
            "(last_run_id IS NOT NULL AND last_run_at IS NOT NULL AND "
            "last_run_status IN ('running', 'succeeded', 'failed', 'skipped'))",
            name="ck_scheduled_tasks_last_run",
        ),
        CheckConstraint(
            "archived_at IS NULL OR archived_at >= created_at",
            name="ck_scheduled_tasks_archived",
        ),
        Index(
            "ix_scheduled_tasks_enabled",
            "enabled",
            "paused",
            "archived_at",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(16), nullable=False)
    cron_expr: Mapped[str | None] = mapped_column(String(255), nullable=True)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("models.id", ondelete="RESTRICT"), nullable=False
    )
    enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    paused: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_run_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_run_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    archived_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=utc_ms_now, onupdate=utc_ms_now
    )


Index(
    "ix_scheduled_tasks_workspace",
    ScheduledTaskORM.workspace_id,
    ScheduledTaskORM.archived_at,
    ScheduledTaskORM.updated_at.desc(),
)


class ScheduledTaskRunORM(Base):
    __tablename__ = "scheduled_task_runs"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_scheduled_task_runs"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'skipped')",
            name="ck_scheduled_task_runs_status",
        ),
        CheckConstraint(
            "status <> 'running' OR "
            "(lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="ck_scheduled_task_runs_lease",
        ),
        Index(
            "uq_scheduled_task_runs_one_running",
            "task_id",
            unique=True,
            sqlite_where=text("status = 'running'"),
        ),
        Index("ix_scheduled_task_runs_turn", "turn_id"),
        Index("ix_scheduled_task_runs_lease", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36))
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scheduled_tasks.id", ondelete="RESTRICT"),
        nullable=False,
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True
    )
    turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default="running"
    )
    lease_owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    lease_expires_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finished_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)


Index(
    "ix_scheduled_task_runs_task_created",
    ScheduledTaskRunORM.task_id,
    ScheduledTaskRunORM.created_at.desc(),
    ScheduledTaskRunORM.id.desc(),
)
Index(
    "ix_scheduled_task_runs_global_created",
    ScheduledTaskRunORM.created_at.desc(),
    ScheduledTaskRunORM.id.desc(),
)
Index(
    "ix_scheduled_task_runs_session",
    ScheduledTaskRunORM.session_id,
    ScheduledTaskRunORM.created_at.desc(),
)
