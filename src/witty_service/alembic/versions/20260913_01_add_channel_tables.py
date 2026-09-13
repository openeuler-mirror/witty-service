"""IM Channel 渠道表与 sessions.origin。

revision      = "20260913_01"
down_revision = "20260903_01"

本迁移只做加表与加列，**不修改任何既有列**，因此向前兼容；`downgrade` 删除本
迁移新增的表与列，不触碰既有数据。

注意：`persistence/db.py::_handle_legacy_db_if_needed` 的必需对象清单必须与
本迁移同步（渠道表 + `sessions.origin`），否则一个"有 agents 表但无
alembic_version"的存量库会在迁移执行前被判为结构完整并 stamp head，导致渠道表
永远不会被创建（实施计划 §4.1）。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260913_01"
down_revision = "20260903_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 既有表变更：会话来源（web / channel:<渠道标识符> / scheduled）
    op.add_column(
        "sessions",
        sa.Column(
            "origin",
            sa.String(length=64),
            nullable=True,
            server_default="web",
        ),
    )

    op.create_table(
        "channel_instances",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("owner_ref", sa.String(length=255), nullable=True),
        # 引用 agents.id，无外键：设 SET NULL 后无法区分"从未绑定"与"绑定后被删除"
        sa.Column("agent_id", sa.String(length=36), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("generation", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("credential_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("credential_mask", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_channel_instances_owner_ref", "channel_instances", ["owner_ref"]
    )
    op.create_index("ix_channel_instances_status", "channel_instances", ["status"])

    op.create_table(
        "channel_provisionings",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("owner_ref", sa.String(length=255), nullable=True),
        sa.Column("agent_id", sa.String(length=36), nullable=True),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="waiting"
        ),
        sa.Column("qr_content", sa.Text(), nullable=True),
        sa.Column("poll_interval_ms", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # 平台侧临时凭据：加密存储，绝不外发
        sa.Column("state_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_channel_provisionings_status_expires",
        "channel_provisionings",
        ["status", "expires_at"],
    )

    op.create_table(
        "channel_routes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("channel_instance_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_type", sa.String(length=16), nullable=False),
        sa.Column("platform_user_id", sa.String(length=255), nullable=False),
        # 引用 sessions.id，无外键：会话可能被控制台删除，渠道层需要感知该删除
        sa.Column("active_session_id", sa.String(length=36), nullable=True),
        sa.Column("active_placeholder_ref", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_instance_id"], ["channel_instances.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_instance_id",
            "conversation_type",
            "platform_user_id",
            name="uq_channel_routes_instance_conversation_user",
        ),
    )
    op.create_index(
        "ix_channel_routes_channel_instance_id", "channel_routes", ["channel_instance_id"]
    )

    op.create_table(
        "channel_inbound_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("channel_instance_id", sa.String(length=36), nullable=False),
        sa.Column("platform_event_id", sa.String(length=255), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_instance_id",
            "platform_event_id",
            name="uq_channel_inbound_events_instance_event",
        ),
    )
    op.create_index(
        "ix_channel_inbound_events_received_at", "channel_inbound_events", ["received_at"]
    )

    op.create_table(
        "channel_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        # 引用列均不设外键：排障表要能在实体被删除后仍然保留证据
        sa.Column("channel_instance_id", sa.String(length=36), nullable=False),
        sa.Column("route_id", sa.String(length=36), nullable=True),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("turn_id", sa.String(length=36), nullable=True),
        sa.Column("certainty", sa.String(length=16), nullable=False),
        sa.Column("segment_index", sa.Integer(), nullable=False),
        sa.Column("platform_message_ref", sa.String(length=255), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_channel_deliveries_created_at", "channel_deliveries", ["created_at"]
    )

    op.create_table(
        "channel_access_policies",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("channel_instance_id", sa.String(length=36), nullable=False),
        sa.Column("conversation_type", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("allowlist", sa.JSON(), nullable=False),
        sa.Column(
            "allow_commands", sa.Boolean(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["channel_instance_id"], ["channel_instances.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "channel_instance_id",
            "conversation_type",
            name="uq_channel_access_policies_instance_conversation",
        ),
    )
    op.create_index(
        "ix_channel_access_policies_channel_instance_id",
        "channel_access_policies",
        ["channel_instance_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_channel_access_policies_channel_instance_id",
        table_name="channel_access_policies",
    )
    op.drop_table("channel_access_policies")

    op.drop_index("ix_channel_deliveries_created_at", table_name="channel_deliveries")
    op.drop_table("channel_deliveries")

    op.drop_index(
        "ix_channel_inbound_events_received_at", table_name="channel_inbound_events"
    )
    op.drop_table("channel_inbound_events")

    op.drop_index("ix_channel_routes_channel_instance_id", table_name="channel_routes")
    op.drop_table("channel_routes")

    op.drop_index(
        "ix_channel_provisionings_status_expires", table_name="channel_provisionings"
    )
    op.drop_table("channel_provisionings")

    op.drop_index("ix_channel_instances_status", table_name="channel_instances")
    op.drop_index("ix_channel_instances_owner_ref", table_name="channel_instances")
    op.drop_table("channel_instances")

    op.drop_column("sessions", "origin")
