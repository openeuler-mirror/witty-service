from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SessionStatus(StrEnum):
    running = "running"
    idle = "idle"
    error = "error"


class MessageStatus(StrEnum):
    generating = "generating"
    completed = "completed"
    error = "error"
    interrupted = "interrupted"


class AgentORM(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sandbox_type: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    sandbox_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)
    idle_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    has_scheduled_tasks: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    model_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mcp_server_list: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    last_active_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class AgentRuntimeStateORM(Base):
    __tablename__ = "agent_runtime_state"

    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    runtime_payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    adapter_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    adapter_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SessionORM(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint(
            "runtime_type",
            "runtime_session_key",
            name="uq_sessions_runtime_type_session_key",
        ),
        UniqueConstraint(
            "runtime_type",
            "runtime_session_id",
            name="uq_sessions_runtime_type_session_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    remote_runtime_agent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    runtime_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    runtime_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    runtime_session_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[SessionStatus] = mapped_column(
        SQLEnum(
            SessionStatus,
            native_enum=False,
            validate_strings=True,
            create_constraint=True,
            name="session_status",
        ),
        nullable=False,
        default=SessionStatus.idle,
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class MessageORM(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_session_created", "session_id", "created_at"),
        Index("ix_messages_session_status", "session_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    status: Mapped[MessageStatus] = mapped_column(
        SQLEnum(
            MessageStatus,
            native_enum=False,
            validate_strings=True,
            create_constraint=True,
            name="message_status",
        ),
        nullable=False,
        default=MessageStatus.completed,
    )
    last_stream_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )


class MessageEventORM(Base):
    __tablename__ = "message_events"
    __table_args__ = (
        UniqueConstraint("session_id", "seq_no", name="uq_message_events_session_seq"),
        Index("ix_message_events_msg_seq", "message_id", "seq_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    seq_no: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )


class AgentLockORM(Base):
    __tablename__ = "agent_locks"

    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ModelORM(Base):
    __tablename__ = "models"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    api_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    compatibility: Mapped[str | None] = mapped_column(String(16), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)
    temperature: Mapped[float] = mapped_column(Integer, nullable=False, default=7)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class SkillRepositoryORM(Base):
    __tablename__ = "skill_repo"

    repo_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repo_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_discover_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="init"
    )
    skill_num: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class SkillORM(Base):
    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint(
            "repo_id", "relative_path", name="uq_skills_repo_relative_path"
        ),
    )

    skill_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repo_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("skill_repo.repo_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    skill_name: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON,
        nullable=False,
        default=dict,
    )
    skill_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    skill_md_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class AgentSkillORM(Base):
    __tablename__ = "agent_skills"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('builtin', 'git', 'local', 'clawhub', 'wittyhub')",
            name="ck_agent_skills_source_type",
        ),
        CheckConstraint(
            "(source_type IN ('git', 'local', 'clawhub') AND repo_id IS NOT NULL) OR "
            "(source_type IN ('builtin', 'wittyhub') AND repo_id IS NULL)",
            name="ck_agent_skills_repo_id_by_source",
        ),
    )

    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    skill_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    repo_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("skill_repo.repo_id", ondelete="SET NULL"),
        nullable=True,
    )
    skill_name: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON,
        nullable=True,
        default=dict,
    )
    skill_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    skill_md_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )


class McpServerORM(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mcp_server_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mcp_server_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )
