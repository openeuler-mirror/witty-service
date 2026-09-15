"""渠道凭据移出主库：ciphertext 列换成不透明引用。

revision      = "20260915_01"
down_revision = "20260913_01"

本迁移只改列，不改任何既有业务表。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260915_01"
down_revision = "20260913_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. 存量实例：凭据即将失去载体，先把状态改成说实话的值
    op.execute(
        sa.text(
            "UPDATE channel_instances SET status = 'error' "
            "WHERE credential_ciphertext IS NOT NULL"
        )
    )

    # 2. 实例凭据列：密文 -> 引用
    op.add_column(
        "channel_instances",
        sa.Column("credential_ref", sa.String(length=64), nullable=True),
    )
    op.drop_column("channel_instances", "credential_ciphertext")

    # 3. 接入尝试的平台临时凭据列：密文 -> 引用
    op.add_column(
        "channel_provisionings",
        sa.Column("state_ref", sa.String(length=64), nullable=True),
    )
    op.drop_column("channel_provisionings", "state_ciphertext")


def downgrade() -> None:
    """回退到"加密落库"的 schema。

    **数据不可回退**：`credential_ref` 指向的文件在降级后不会被读回（旧代码只会
    读密文列），因此降级后所有实例同样需要重新接入。这里只恢复列本身。
    """
    op.add_column(
        "channel_provisionings",
        sa.Column("state_ciphertext", sa.LargeBinary(), nullable=True),
    )
    op.drop_column("channel_provisionings", "state_ref")

    op.add_column(
        "channel_instances",
        sa.Column("credential_ciphertext", sa.LargeBinary(), nullable=True),
    )
    op.drop_column("channel_instances", "credential_ref")
