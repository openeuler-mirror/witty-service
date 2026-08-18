from __future__ import annotations

from alembic import op

revision = "20260814_01"
down_revision = "20260804_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 兜底：历史遗留的 'pending' 状态（旧默认值）若存在，先统一归一为 'running'
    op.execute("UPDATE scheduled_task_runs SET status='running' WHERE status='pending'")

    # 先清理历史遗留的悬挂 session_id，避免在开启 PRAGMA foreign_keys=ON 的
    # SQLite 上重建表时因外键约束失败（旧实现允许运行记录引用已删除会话）。
    op.execute(
        "UPDATE scheduled_task_runs "
        "SET session_id = NULL "
        "WHERE session_id IS NOT NULL "
        "AND session_id NOT IN (SELECT id FROM sessions)"
    )

    with op.batch_alter_table("scheduled_task_runs", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_scheduled_task_runs_status", type_="check")
        batch_op.create_check_constraint(
            "ck_scheduled_task_runs_status",
            "status IN ('running', 'succeeded', 'failed', 'skipped')",
        )
        batch_op.alter_column("status", server_default=None)
        batch_op.create_foreign_key(
            "fk_scheduled_task_runs_session_id",
            "sessions",
            ["session_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index("ix_scheduled_task_runs_session_id", ["session_id"])

    op.create_index(
        "ix_scheduled_task_runs_created_at",
        "scheduled_task_runs",
        ["created_at"],
    )

    # 使用原生 ALTER TABLE DROP COLUMN：SQLite 的 batch_alter_table 重建 agents 表时，
    # 若 PRAGMA foreign_keys=ON，DROP TABLE 会触发子表 ON DELETE CASCADE，
    # 导致 sessions / scheduled_tasks / messages 等子表数据被级联清空。
    op.execute("ALTER TABLE agents DROP COLUMN has_scheduled_tasks")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE agents ADD COLUMN has_scheduled_tasks BOOLEAN NOT NULL DEFAULT 0"
    )

    op.drop_index("ix_scheduled_task_runs_created_at", "scheduled_task_runs")

    with op.batch_alter_table("scheduled_task_runs", recreate="always") as batch_op:
        batch_op.drop_index("ix_scheduled_task_runs_session_id")
        batch_op.drop_constraint(
            "fk_scheduled_task_runs_session_id", type_="foreignkey"
        )
        batch_op.drop_constraint("ck_scheduled_task_runs_status", type_="check")
        batch_op.create_check_constraint(
            "ck_scheduled_task_runs_status",
            "status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')",
        )
        batch_op.alter_column("status", server_default="pending")
