from __future__ import annotations

from sqlalchemy import (
    JSON,
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


class ChannelInstanceORM(Base):
    __tablename__ = "channel_instances"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_channel_instances"),
        CheckConstraint(
            "status IN ('pending', 'connected', 'degraded', 'offline', 'error', "
            "'disabled')",
            name="ck_channel_instances_status",
        ),
        CheckConstraint("paused IN (0, 1)", name="ck_channel_instances_paused"),
        CheckConstraint(
            "(paused = 0 AND reason IS NULL) OR (paused = 1 AND reason IS NOT NULL)",
            name="ck_channel_instances_pause",
        ),
        CheckConstraint("generation >= 1", name="ck_channel_instances_generation"),
        CheckConstraint(
            "json_valid(config_json)", name="ck_channel_instances_config_json"
        ),
        CheckConstraint(
            "(workspace_id IS NULL AND agent_id IS NULL AND model_id IS NULL) OR "
            "(workspace_id IS NOT NULL AND agent_id IS NOT NULL AND "
            "model_id IS NOT NULL)",
            name="ck_channel_instances_binding",
        ),
        Index("ix_channel_instances_status", "status", "paused", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36))
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    owner_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=True
    )
    agent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    model_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("models.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    paused: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    generation: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    config_json: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    credential_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credential_mask: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=utc_ms_now, onupdate=utc_ms_now
    )


Index(
    "ix_channel_instances_owner",
    ChannelInstanceORM.owner_ref,
    ChannelInstanceORM.channel,
    ChannelInstanceORM.status,
    ChannelInstanceORM.created_at.desc(),
    ChannelInstanceORM.id,
)
Index(
    "ix_channel_instances_binding",
    ChannelInstanceORM.workspace_id,
    ChannelInstanceORM.agent_id,
    ChannelInstanceORM.created_at.desc(),
)


class ChannelProvisioningORM(Base):
    __tablename__ = "channel_provisionings"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_channel_provisionings"),
        CheckConstraint(
            "status IN ('waiting', 'succeeded', 'expired', 'failed', 'cancelled')",
            name="ck_channel_provisionings_status",
        ),
        CheckConstraint(
            "poll_interval_ms >= 250",
            name="ck_channel_provisionings_poll_interval_ms",
        ),
        CheckConstraint(
            "(workspace_id IS NULL AND agent_id IS NULL AND model_id IS NULL) OR "
            "(workspace_id IS NOT NULL AND agent_id IS NOT NULL AND "
            "model_id IS NOT NULL)",
            name="ck_channel_provisionings_binding",
        ),
        Index(
            "ix_channel_provisionings_lookup",
            "channel",
            "owner_ref",
            "status",
            "expires_at",
        ),
        Index("ix_channel_provisionings_expiry", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36))
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=True
    )
    agent_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    model_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("models.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="waiting", server_default="waiting"
    )
    qr_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    poll_interval_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2000, server_default="2000"
    )
    expires_at: Mapped[int] = mapped_column(Integer, nullable=False)
    state_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=utc_ms_now, onupdate=utc_ms_now
    )


Index(
    "uq_channel_provisionings_waiting",
    ChannelProvisioningORM.channel,
    func.coalesce(ChannelProvisioningORM.owner_ref, literal("", literal_execute=True)),
    unique=True,
    sqlite_where=text("status = 'waiting'"),
)


class ChannelRouteORM(Base):
    __tablename__ = "channel_routes"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_channel_routes"),
        CheckConstraint(
            "conversation_type IN ('direct', 'group')",
            name="ck_channel_routes_conversation_type",
        ),
        UniqueConstraint(
            "channel_instance_id",
            "conversation_type",
            "platform_user_id",
            name="uq_channel_routes_instance_conversation_user",
        ),
    )

    id: Mapped[str] = mapped_column(String(36))
    channel_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("channel_instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    platform_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    active_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    active_placeholder_ref: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=utc_ms_now, onupdate=utc_ms_now
    )


Index(
    "ix_channel_routes_active_session",
    ChannelRouteORM.active_session_id,
)


class ChannelInboundEventORM(Base):
    __tablename__ = "channel_inbound_events"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_channel_inbound_events"),
        UniqueConstraint(
            "channel_instance_id",
            "platform_event_id",
            name="uq_channel_inbound_events_instance_event",
        ),
        Index("ix_channel_inbound_events_received", "received_at"),
    )

    id: Mapped[str] = mapped_column(String(36))
    channel_instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    platform_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[int] = mapped_column(Integer, nullable=False)


class ChannelDeliveryORM(Base):
    __tablename__ = "channel_deliveries"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_channel_deliveries"),
        CheckConstraint(
            "certainty IN ('delivered', 'rejected', 'uncertain')",
            name="ck_channel_deliveries_certainty",
        ),
        CheckConstraint(
            "segment_index >= 0", name="ck_channel_deliveries_segment_index"
        ),
    )

    id: Mapped[str] = mapped_column(String(36))
    channel_instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    route_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    certainty: Mapped[str] = mapped_column(String(16), nullable=False)
    segment_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    platform_message_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, default=utc_ms_now)


Index(
    "ix_channel_deliveries_instance_created",
    ChannelDeliveryORM.channel_instance_id,
    ChannelDeliveryORM.created_at.desc(),
    ChannelDeliveryORM.id,
)
Index(
    "ix_channel_deliveries_route_created",
    ChannelDeliveryORM.route_id,
    ChannelDeliveryORM.created_at.desc(),
    ChannelDeliveryORM.id,
)
Index(
    "ix_channel_deliveries_turn_created",
    ChannelDeliveryORM.turn_id,
    ChannelDeliveryORM.created_at.desc(),
    ChannelDeliveryORM.id,
)
Index("ix_channel_deliveries_created", ChannelDeliveryORM.created_at)


class ChannelAccessPolicyORM(Base):
    __tablename__ = "channel_access_policies"
    __table_args__ = (
        PrimaryKeyConstraint("id", name="pk_channel_access_policies"),
        CheckConstraint(
            "conversation_type IN ('direct', 'group')",
            name="ck_channel_access_policies_conversation_type",
        ),
        CheckConstraint(
            "mode IN ('open', 'allowlist')",
            name="ck_channel_access_policies_mode",
        ),
        CheckConstraint(
            "json_valid(allowlist_json) AND json_type(allowlist_json) = 'array'",
            name="ck_channel_access_policies_allowlist_json",
        ),
        CheckConstraint(
            "allow_commands IN (0, 1)",
            name="ck_channel_access_policies_allow_commands",
        ),
        UniqueConstraint(
            "channel_instance_id",
            "conversation_type",
            name="uq_channel_access_policies_instance_conversation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36))
    channel_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("channel_instances.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="open", server_default="open"
    )
    allowlist_json: Mapped[list[object]] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    allow_commands: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    updated_at: Mapped[int] = mapped_column(
        Integer, nullable=False, default=utc_ms_now, onupdate=utc_ms_now
    )
