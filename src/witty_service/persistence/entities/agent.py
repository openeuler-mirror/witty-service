from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
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


class AgentDefinitionORM(Base):
    __tablename__ = "agent_definitions"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_agent_definitions"),
        CheckConstraint(
            "kind IN ('primary', 'expert')", name="ck_agent_definitions_kind"
        ),
        CheckConstraint(
            "source_type IN ('local', 'market', 'preset')",
            name="ck_agent_definitions_source_type",
        ),
        UniqueConstraint("name", name="uq_agent_definitions_name"),
        Index(
            "uq_agent_definitions_one_primary",
            "kind",
            unique=True,
            sqlite_where=text("kind = 'primary' AND archived_at IS NULL"),
        ),
        Index("ix_agent_definitions_catalog", "kind", "archived_at", "name"),
    )

    id: Mapped[str] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255, collation="NOCASE"), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    system_prompt: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    config_updated_at: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="local", server_default="local"
    )
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    archived_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=utc_ms_now, onupdate=utc_ms_now
    )
