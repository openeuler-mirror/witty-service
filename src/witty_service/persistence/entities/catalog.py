from __future__ import annotations

from sqlalchemy import (
    JSON,
    REAL,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    literal,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from witty_service.persistence.entities.base import Base, utc_ms_now


class ModelORM(Base):
    __tablename__ = "models"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_models"),
        CheckConstraint("enabled IN (0, 1)", name="ck_models_enabled"),
        CheckConstraint("is_default IN (0, 1)", name="ck_models_is_default"),
        CheckConstraint("max_tokens > 0", name="ck_models_max_tokens"),
        CheckConstraint(
            "temperature >= 0.0 AND temperature <= 2.0",
            name="ck_models_temperature",
        ),
        Index(
            "uq_models_single_default",
            "is_default",
            unique=True,
            sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36))
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    compatibility: Mapped[str | None] = mapped_column(String(32), nullable=True)
    api_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    credential_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    max_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=32768, server_default="32768"
    )
    temperature: Mapped[float] = mapped_column(
        REAL, nullable=False, default=0.7, server_default="0.7"
    )
    is_default: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=utc_ms_now, onupdate=utc_ms_now
    )


Index(
    "uq_models_provider_endpoint_model",
    ModelORM.provider_id,
    func.coalesce(ModelORM.api_base_url, literal("", literal_execute=True)),
    ModelORM.provider_model_id,
    unique=True,
)
Index(
    "ix_models_catalog",
    ModelORM.enabled,
    ModelORM.is_default.desc(),
    ModelORM.created_at.desc(),
    ModelORM.id,
)


class SkillRepoORM(Base):
    __tablename__ = "skill_repo"
    __table_args__ = (
        PrimaryKeyConstraint("repo_id", name="pk_skill_repo"),
        CheckConstraint(
            "skill_discover_status IN ('init', 'discovering', 'ready', 'error')",
            name="ck_skill_repo_skill_discover_status",
        ),
        CheckConstraint("skill_count >= 0", name="ck_skill_repo_skill_count"),
        UniqueConstraint("repo_name", name="uq_skill_repo_repo_name"),
    )

    repo_id: Mapped[str] = mapped_column(String(36))
    repo_name: Mapped[str] = mapped_column(
        String(255, collation="NOCASE"), nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_discover_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="init", server_default="init"
    )
    skill_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=utc_ms_now, onupdate=utc_ms_now
    )


Index(
    "ix_skill_repo_status",
    SkillRepoORM.skill_discover_status,
    SkillRepoORM.updated_at,
)


class SkillORM(Base):
    __tablename__ = "skills"
    __table_args__ = (
        PrimaryKeyConstraint("skill_id", name="pk_skills"),
        CheckConstraint("enabled IN (0, 1)", name="ck_skills_enabled"),
        CheckConstraint("json_valid(metadata_json)", name="ck_skills_metadata_json"),
        UniqueConstraint("skill_name", name="uq_skills_skill_name"),
        UniqueConstraint("repo_id", "relative_path", name="uq_skills_repo_path"),
    )

    skill_id: Mapped[str] = mapped_column(String(36))
    repo_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("skill_repo.repo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    skill_name: Mapped[str] = mapped_column(
        String(255, collation="NOCASE"), nullable=False
    )
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    skill_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    skill_md_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=utc_ms_now, onupdate=utc_ms_now
    )


Index("ix_skills_enabled_name", SkillORM.enabled, SkillORM.skill_name)


class McpServerORM(Base):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_mcp_servers"),
        CheckConstraint("enabled IN (0, 1)", name="ck_mcp_servers_enabled"),
        CheckConstraint("secret_revision >= 0", name="ck_mcp_servers_secret_revision"),
        CheckConstraint("json_valid(config_json)", name="ck_mcp_servers_config_json"),
        UniqueConstraint("mcp_server_name", name="uq_mcp_servers_mcp_server_name"),
    )

    id: Mapped[str] = mapped_column(String(36))
    mcp_server_name: Mapped[str] = mapped_column(
        String(255, collation="NOCASE"), nullable=False
    )
    config_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    credential_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    secret_revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=utc_ms_now, onupdate=utc_ms_now
    )


Index("ix_mcp_servers_enabled_name", McpServerORM.enabled, McpServerORM.mcp_server_name)
