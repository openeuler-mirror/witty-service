from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from witty_service.persistence.entities.base import Base, utc_ms_now


class WorkspaceORM(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_workspaces"),
        CheckConstraint(
            "sandbox_type IN ('local', 'docker')", name="ck_workspaces_sandbox_type"
        ),
        CheckConstraint("is_default IN (0, 1)", name="ck_workspaces_is_default"),
        CheckConstraint("session_count >= 0", name="ck_workspaces_session_count"),
        UniqueConstraint("host_directory", name="uq_workspaces_host_directory"),
        Index(
            "uq_workspaces_one_default",
            "is_default",
            unique=True,
            sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36))
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    host_directory: Mapped[str] = mapped_column(Text, nullable=False)
    sandbox_type: Mapped[str] = mapped_column(String(16), nullable=False)
    default_agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    default_model_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("models.id", ondelete="RESTRICT"), nullable=True
    )
    is_default: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    published_projection_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    session_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_activity_at: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=utc_ms_now, onupdate=utc_ms_now
    )


Index(
    "ix_workspaces_sidebar",
    WorkspaceORM.is_default.desc(),
    WorkspaceORM.last_activity_at.desc(),
    WorkspaceORM.id.desc(),
)
