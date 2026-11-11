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


class TurnORM(Base):
    __tablename__ = "turns"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_turns"),
        CheckConstraint("turn_seq >= 1", name="ck_turns_turn_seq"),
        CheckConstraint(
            "status IN ('running', 'completed', 'error', 'interrupted')",
            name="ck_turns_status",
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL) OR "
            "(status <> 'running' AND finished_at IS NOT NULL)",
            name="ck_turns_terminal_time",
        ),
        UniqueConstraint("session_id", "turn_seq", name="uq_turns_session_seq"),
        Index(
            "uq_turns_one_running",
            "session_id",
            unique=True,
            sqlite_where=text("status = 'running'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36))
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    turn_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("models.id", ondelete="RESTRICT"), nullable=False
    )
    agent_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    model_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", server_default="running"
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[int] = mapped_column(Integer, nullable=False)
    finished_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)


Index("ix_turns_agent", TurnORM.agent_id, TurnORM.created_at.desc())


class TurnOperationORM(Base):
    __tablename__ = "turn_operations"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_turn_operations"),
        CheckConstraint("type IN ('regenerate')", name="ck_turn_operations_type"),
        CheckConstraint(
            "status IN ('reverting', 'accepted', 'failed', 'interrupted')",
            name="ck_turn_operations_status",
        ),
        Index(
            "uq_turn_operations_one_reverting",
            "session_id",
            unique=True,
            sqlite_where=text("status = 'reverting'"),
        ),
        Index("ix_turn_operations_target", "target_turn_id"),
    )

    id: Mapped[str] = mapped_column(String(36))
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    target_turn_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="regenerate", server_default="regenerate"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="reverting", server_default="reverting"
    )
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=utc_ms_now, onupdate=utc_ms_now
    )


Index(
    "ix_turn_operations_session_created",
    TurnOperationORM.session_id,
    TurnOperationORM.created_at.desc(),
    TurnOperationORM.id.desc(),
)


class TurnRuntimeMessageORM(Base):
    __tablename__ = "turn_runtime_messages"
    __table_args__ = (
        PrimaryKeyConstraint("runtime_message_id", name="pk_turn_runtime_messages"),
        CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_turn_runtime_messages_role",
        ),
        CheckConstraint("ordinal >= 0", name="ck_turn_runtime_messages_ordinal"),
        UniqueConstraint(
            "turn_id", "ordinal", name="uq_turn_runtime_messages_turn_ordinal"
        ),
    )

    runtime_message_id: Mapped[str] = mapped_column(String(255))
    turn_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)
