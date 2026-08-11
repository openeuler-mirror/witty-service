from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260804_01"
down_revision = "20260707_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("schedule_type", sa.String(length=16), nullable=False),
        sa.Column("cron_expr", sa.String(length=255), nullable=True),
        sa.Column("interval_seconds", sa.Integer(), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("agent_id", sa.String(length=36), nullable=False),
        sa.Column("workspace_folder", sa.String(length=512), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "schedule_type IN ('cron', 'interval')",
            name="ck_scheduled_tasks_schedule_type",
        ),
        sa.CheckConstraint(
            "(schedule_type = 'cron' AND cron_expr IS NOT NULL AND "
            "interval_seconds IS NULL) OR "
            "(schedule_type = 'interval' AND interval_seconds IS NOT NULL AND "
            "cron_expr IS NULL)",
            name="ck_scheduled_tasks_schedule_fields",
        ),
        sa.ForeignKeyConstraint(["agent_id"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduled_tasks_agent_id",
        "scheduled_tasks",
        ["agent_id"],
    )

    op.create_table(
        "scheduled_task_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')",
            name="ck_scheduled_task_runs_status",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["scheduled_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_scheduled_task_runs_task_id",
        "scheduled_task_runs",
        ["task_id"],
    )
    op.create_index(
        "ix_scheduled_task_runs_task_created",
        "scheduled_task_runs",
        ["task_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_scheduled_task_runs_task_created", table_name="scheduled_task_runs"
    )
    op.drop_index("ix_scheduled_task_runs_task_id", table_name="scheduled_task_runs")
    op.drop_table("scheduled_task_runs")
    op.drop_index("ix_scheduled_tasks_agent_id", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
