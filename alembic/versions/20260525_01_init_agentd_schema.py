from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260525_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("sandbox_type", sa.String(length=32), nullable=False),
        sa.Column("adapter_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("sandbox_id", sa.String(length=255), nullable=True),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column("idle_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("has_scheduled_tasks", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "agent_locks",
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("lock_version", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_id"),
    )

    op.create_table(
        "agent_runtime_state",
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("runtime_payload_json", sa.JSON(), nullable=False),
        sa.Column("adapter_base_url", sa.String(length=255), nullable=True),
        sa.Column("adapter_ready", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("agent_id"),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("remote_runtime_agent_id", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "idle",
                "error",
                name="session_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default="idle",
        ),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_sessions_agent_id", "sessions", ["agent_id"], unique=False)

    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "generating",
                "completed",
                "error",
                "interrupted",
                name="message_status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
            server_default="completed",
        ),
        sa.Column("last_stream_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_messages_agent_id", "messages", ["agent_id"], unique=False)
    op.create_index("ix_messages_session_id", "messages", ["session_id"], unique=False)
    op.create_index("ix_messages_session_created", "messages", ["session_id", "created_at"])
    op.create_index("ix_messages_session_status", "messages", ["session_id", "status"])

    op.create_table(
        "message_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=False),
        sa.Column("message_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("seq_no", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "seq_no", name="uq_message_events_session_seq"),
    )
    op.create_index("ix_message_events_agent_id", "message_events", ["agent_id"], unique=False)
    op.create_index("ix_message_events_message_id", "message_events", ["message_id"], unique=False)
    op.create_index("ix_message_events_session_id", "message_events", ["session_id"], unique=False)
    op.create_index("ix_message_events_msg_seq", "message_events", ["message_id", "seq_no"])

    op.create_table(
        "skill_repo",
        sa.Column("repo_id", sa.String(length=36), nullable=False),
        sa.Column("repo_name", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("skill_discover_status", sa.String(length=32), nullable=False, server_default="init"),
        sa.Column("skill_num", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("repo_id"),
    )
    op.create_table(
        "skills",
        sa.Column("skill_id", sa.String(length=36), nullable=False),
        sa.Column("repo_id", sa.String(length=36), nullable=True),
        sa.Column("skill_name", sa.String(length=255), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("skill_source", sa.String(length=255), nullable=True),
        sa.Column("skill_md_url", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["repo_id"], ["skill_repo.repo_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("skill_id"),
        sa.UniqueConstraint("repo_id", "relative_path", name="uq_skills_repo_relative_path"),
    )
    op.create_index("ix_skills_repo_id", "skills", ["repo_id"], unique=False)
    op.create_table(
        "agent_skills",
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("skill_id", sa.String(length=36), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("repo_id", sa.String(length=36), nullable=True),
        sa.Column("skill_name", sa.String(length=255), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('builtin', 'git', 'local')",
            name="ck_agent_skills_source_type",
        ),
        sa.CheckConstraint(
            "(source_type = 'builtin' AND repo_id IS NULL) OR "
            "(source_type IN ('git', 'local') AND repo_id IS NOT NULL)",
            name="ck_agent_skills_repo_id_by_source",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["repo_id"], ["skill_repo.repo_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("agent_id", "skill_id"),
    )

    op.create_table(
        "models",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("api_key", sa.Text(), nullable=False),
        sa.Column("api_base_url", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="4096"),
        sa.Column("temperature", sa.Integer(), nullable=False, server_default="7"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("models")
    
    op.drop_table("agent_skills")
    op.drop_table("skills")
    op.drop_table("skill_repo")

    op.drop_index("ix_message_events_session_id", table_name="message_events")
    op.drop_index("ix_message_events_message_id", table_name="message_events")
    op.drop_index("ix_message_events_agent_id", table_name="message_events")
    op.drop_table("message_events")

    op.drop_index("ix_messages_session_id", table_name="messages")
    op.drop_index("ix_messages_agent_id", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_sessions_agent_id", table_name="sessions")
    op.drop_table("sessions")

    op.drop_table("agent_runtime_state")
    op.drop_table("agent_locks")
    op.drop_table("agents")
