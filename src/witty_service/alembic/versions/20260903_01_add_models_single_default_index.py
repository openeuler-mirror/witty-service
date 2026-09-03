from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260903_01"
down_revision = "20260814_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 部分唯一索引——至多一个模型 is_default=1(仅索引 is_default=1 的行)。
    # 这样"并发置为默认"也由 DB 原子兜底,应用层只需做"先清后写"的切换。
    op.create_index(
        "uq_models_single_default",
        "models",
        ["is_default"],
        unique=True,
        sqlite_where=sa.text("is_default = 1"),
    )


def downgrade() -> None:
    op.drop_index("uq_models_single_default", "models")
