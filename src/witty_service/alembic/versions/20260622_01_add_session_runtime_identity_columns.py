from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260622_01"
down_revision = "20260525_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.add_column(sa.Column("runtime_type", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("runtime_session_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("runtime_session_key", sa.Text(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_sessions_runtime_type_session_key",
            ["runtime_type", "runtime_session_key"],
        )
        batch_op.create_unique_constraint(
            "uq_sessions_runtime_type_session_id",
            ["runtime_type", "runtime_session_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("sessions") as batch_op:
        batch_op.drop_constraint(
            "uq_sessions_runtime_type_session_id",
            type_="unique",
        )
        batch_op.drop_constraint(
            "uq_sessions_runtime_type_session_key",
            type_="unique",
        )
        batch_op.drop_column("runtime_session_key")
        batch_op.drop_column("runtime_session_id")
        batch_op.drop_column("runtime_type")
