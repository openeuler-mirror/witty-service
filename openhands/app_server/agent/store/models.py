"""SQLAlchemy models for agent persistence."""

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from openhands.app_server.utils.sql_utils import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentdAgent(Base):
    __tablename__ = "agentd_agents"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="CREATING")
    sandbox_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    default_session_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    template: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    model_override: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    sandbox_config: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    idle_timeout: Mapped[int] = mapped_column(Integer, default=300)
    has_scheduled_tasks: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )

    sessions: Mapped[list["AgentdSession"]] = relationship(
        "AgentdSession",
        back_populates="agent",
        cascade="all, delete-orphan",
    )

    __table_args__ = ()


class AgentdSession(Base):
    __tablename__ = "agentd_sessions"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    agent_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agentd_agents.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(50), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )
    closed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    agent: Mapped["AgentdAgent"] = relationship("AgentdAgent", back_populates="sessions")
    messages: Mapped[list["AgentdSessionMessage"]] = relationship(
        "AgentdSessionMessage",
        back_populates="session",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_agentd_sessions_agent_id", "agent_id"),
    )


class AgentdSessionMessage(Base):
    __tablename__ = "agentd_session_messages"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("agentd_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(50), default="text")
    attachments: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    extra_data: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
    )

    session: Mapped["AgentdSession"] = relationship(
        "AgentdSession",
        back_populates="messages",
    )

    __table_args__ = (
        Index("idx_agentd_session_messages_session_id", "session_id"),
        Index("idx_agentd_session_messages_created_at", "created_at"),
    )