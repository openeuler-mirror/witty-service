from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from witty_service.persistence.entities.base import Base, utc_ms_now


class SessionORM(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_sessions"),
        CheckConstraint(
            "origin IN ('web', 'channel', 'scheduled')", name="ck_sessions_origin"
        ),
        CheckConstraint(
            "(origin = 'scheduled' AND scheduled_task_id IS NOT NULL) OR "
            "(origin <> 'scheduled' AND scheduled_task_id IS NULL)",
            name="ck_sessions_scheduled_origin",
        ),
        CheckConstraint("last_turn_seq >= 0", name="ck_sessions_last_turn_seq"),
        CheckConstraint("pinned IN (0, 1)", name="ck_sessions_pinned"),
        UniqueConstraint("runtime_session_id", name="uq_sessions_runtime_session_id"),
    )

    id: Mapped[str] = mapped_column(String(36))
    workspace_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    current_agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    current_model_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("models.id", ondelete="RESTRICT"), nullable=True
    )
    runtime_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_turn_seq: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pinned: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default="web", server_default="web"
    )
    scheduled_task_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("scheduled_tasks.id", ondelete="RESTRICT"),
        nullable=True,
    )
    last_activity_at: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=utc_ms_now, onupdate=utc_ms_now
    )


Index(
    "ix_sessions_workspace_sidebar",
    SessionORM.workspace_id,
    SessionORM.deleted_at,
    SessionORM.pinned.desc(),
    SessionORM.last_activity_at.desc(),
    SessionORM.id.desc(),
)
Index(
    "ix_sessions_scheduled_task",
    SessionORM.scheduled_task_id,
    SessionORM.created_at.desc(),
)
