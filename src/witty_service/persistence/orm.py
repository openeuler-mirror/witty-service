from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Enum as SQLEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from witty_service.domain.enums import ScheduledTaskRunStatus

_SCHEDULED_TASK_RUN_STATUS_VALUES = ", ".join(
    f"'{status.value}'" for status in ScheduledTaskRunStatus
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class SessionStatus(StrEnum):
    running = "running"
    idle = "idle"
    error = "error"


class MessageStatus(StrEnum):
    generating = "generating"
    completed = "completed"
    error = "error"
    interrupted = "interrupted"


class AgentORM(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sandbox_type: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    sandbox_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False)
    idle_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    model_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    mcp_server_list: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    last_active_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class AgentRuntimeStateORM(Base):
    __tablename__ = "agent_runtime_state"

    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    runtime_payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    adapter_base_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    adapter_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SessionORM(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint(
            "runtime_type",
            "runtime_session_key",
            name="uq_sessions_runtime_type_session_key",
        ),
        UniqueConstraint(
            "runtime_type",
            "runtime_session_id",
            name="uq_sessions_runtime_type_session_id",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    remote_runtime_agent_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    runtime_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    runtime_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    runtime_session_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[SessionStatus] = mapped_column(
        SQLEnum(
            SessionStatus,
            native_enum=False,
            validate_strings=True,
            create_constraint=True,
            name="session_status",
        ),
        nullable=False,
        default=SessionStatus.idle,
    )
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scheduled_task_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("scheduled_tasks.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # 会话来源：web（控制台，默认）/ channel:<渠道标识符> / scheduled（定时任务）
    origin: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        default="web",
        server_default="web",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class MessageORM(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_session_created", "session_id", "created_at"),
        Index("ix_messages_session_status", "session_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    status: Mapped[MessageStatus] = mapped_column(
        SQLEnum(
            MessageStatus,
            native_enum=False,
            validate_strings=True,
            create_constraint=True,
            name="message_status",
        ),
        nullable=False,
        default=MessageStatus.completed,
    )
    last_stream_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )


class MessageEventORM(Base):
    __tablename__ = "message_events"
    __table_args__ = (
        UniqueConstraint("session_id", "seq_no", name="uq_message_events_session_seq"),
        Index("ix_message_events_msg_seq", "message_id", "seq_no"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    seq_no: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )


class AgentLockORM(Base):
    __tablename__ = "agent_locks"

    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    lock_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ModelORM(Base):
    __tablename__ = "models"

    # B1: 至多一个模型 is_default=1 的 DB 级约束(部分唯一索引)。
    # 需要"先清后写"配合(见 SqliteRepository._clear_other_default_models)。
    __table_args__ = (
        Index(
            "uq_models_single_default",
            "is_default",
            unique=True,
            sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    api_key: Mapped[str] = mapped_column(Text, nullable=False)
    api_base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    compatibility: Mapped[str | None] = mapped_column(String(16), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=4096)
    temperature: Mapped[float] = mapped_column(Integer, nullable=False, default=7)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class SkillRepositoryORM(Base):
    __tablename__ = "skill_repo"

    repo_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repo_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    skill_discover_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="init"
    )
    skill_num: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class SkillORM(Base):
    __tablename__ = "skills"
    __table_args__ = (
        UniqueConstraint(
            "repo_id", "relative_path", name="uq_skills_repo_relative_path"
        ),
    )

    skill_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repo_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("skill_repo.repo_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    skill_name: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON,
        nullable=False,
        default=dict,
    )
    skill_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    skill_md_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class AgentSkillORM(Base):
    __tablename__ = "agent_skills"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('builtin', 'git', 'local', 'clawhub', 'wittyhub')",
            name="ck_agent_skills_source_type",
        ),
        CheckConstraint(
            "(source_type IN ('git', 'local', 'clawhub') AND repo_id IS NOT NULL) OR "
            "(source_type IN ('builtin', 'wittyhub') AND repo_id IS NULL)",
            name="ck_agent_skills_repo_id_by_source",
        ),
    )

    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    skill_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    repo_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("skill_repo.repo_id", ondelete="SET NULL"),
        nullable=True,
    )
    skill_name: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSON,
        nullable=True,
        default=dict,
    )
    skill_source: Mapped[str | None] = mapped_column(String(255), nullable=True)
    skill_md_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )


class McpServerORM(Base):
    __tablename__ = "mcp_servers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    mcp_server_name: Mapped[str] = mapped_column(String(255), nullable=False)
    mcp_server_config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class ScheduledTaskORM(Base):
    """定时任务定义（witty-service 统一调度的事实源）。"""

    __tablename__ = "scheduled_tasks"
    __table_args__ = (
        CheckConstraint(
            "schedule_type IN ('cron', 'interval')",
            name="ck_scheduled_tasks_schedule_type",
        ),
        CheckConstraint(
            "(schedule_type = 'cron' AND cron_expr IS NOT NULL AND "
            "interval_seconds IS NULL) OR "
            "(schedule_type = 'interval' AND interval_seconds IS NOT NULL AND "
            "cron_expr IS NULL)",
            name="ck_scheduled_tasks_schedule_fields",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(16), nullable=False)
    cron_expr: Mapped[str | None] = mapped_column(String(255), nullable=True)
    interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    agent_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("agents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    workspace_folder: Mapped[str | None] = mapped_column(String(512), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
    )


class ScheduledTaskRunORM(Base):
    """定时任务单次运行记录。"""

    __tablename__ = "scheduled_task_runs"
    __table_args__ = (
        Index("ix_scheduled_task_runs_task_created", "task_id", "created_at"),
        Index("ix_scheduled_task_runs_created_at", "created_at"),
        CheckConstraint(
            f"status IN ({_SCHEDULED_TASK_RUN_STATUS_VALUES})",
            name="ck_scheduled_task_runs_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scheduled_tasks.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    session_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ScheduledTaskRunStatus.running
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
    )


# ==============================================================================
# IM Channel 渠道表（框架设计 §5.1）
#
# 三处刻意的取舍（见框架设计 §5.1 的说明）：
# 1. channel_instances.agent_id 不设外键——设 ON DELETE SET NULL 后无法区分
#    "从未绑定"与"绑定后被删除"；
# 2. channel_deliveries 的三列引用不设外键——它是排障表，实体被删除后仍要留证据；
# 3. channel_routes.active_session_id 不设外键——会话可能被控制台删除，渠道层
#    需要感知这次删除（下一条消息重建会话），而不是让数据库静默改写路由状态。
# ==============================================================================


class ChannelInstanceStatus(StrEnum):
    """渠道实例状态（框架设计 §5.1）。"""

    pending = "pending"
    connected = "connected"
    degraded = "degraded"
    offline = "offline"
    error = "error"
    disabled = "disabled"


class ProvisioningStatus(StrEnum):
    """接入尝试状态（框架设计 §5.1）。"""

    waiting = "waiting"
    succeeded = "succeeded"
    expired = "expired"
    failed = "failed"
    cancelled = "cancelled"


class ChannelInstanceORM(Base):
    """渠道实例：某个渠道上一个具体机器人的凭据、运行配置与准入配置的整体。"""

    __tablename__ = "channel_instances"
    __table_args__ = (
        Index("ix_channel_instances_owner_ref", "owner_ref"),
        Index("ix_channel_instances_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 取值域 = ADAPTER_REGISTRY 的键；代码层约定，数据库层不加 CHECK
    # （SQLite 加 CHECK 会让后续新增渠道需要迁移）
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 调用方传入的不透明归属标签：原样保存、原样返回，不解析、不据此鉴权
    owner_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 引用 agents.id，**无外键**（见文件头说明 1）
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ChannelInstanceStatus.pending.value
    )
    # 实例世代：实例被删除后重建时递增，用于丢弃旧世代的在途回调
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    config: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    # 凭据**不在本表里**：这里只有一个不透明引用，凭据本体在服务用户的
    # 0600 文件里（channels/credential_store.py、ADR 0004）
    credential_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credential_mask: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class ChannelProvisioningORM(Base):
    """接入尝试：一次扫码接入从开始到成功、失败或过期之间的过程状态。"""

    __tablename__ = "channel_provisionings"
    __table_args__ = (
        Index("ix_channel_provisionings_status_expires", "status", "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ProvisioningStatus.waiting.value
    )
    # 二维码内容：非密，可展示
    qr_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    poll_interval_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=2000)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # 平台侧临时凭据：存在主库之外（0600 文件）、绝不外发；
    # 接入结束（成功/失败/取消/过期）即清除引用并删除文件
    state_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class ChannelRouteORM(Base):
    """会话路由：路由 → 活动会话的绑定，以及当前占位消息引用。"""

    __tablename__ = "channel_routes"
    __table_args__ = (
        UniqueConstraint(
            "channel_instance_id",
            "conversation_type",
            "platform_user_id",
            name="uq_channel_routes_instance_conversation_user",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("channel_instances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    platform_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    # 引用 sessions.id，**无外键**（见文件头说明 3）
    active_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 当前占位消息的平台引用；终稿投递完成后清空（框架设计 §3.6）
    active_placeholder_ref: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class ChannelInboundEventORM(Base):
    """入站事件去重登记：(渠道实例, 平台事件标识) 唯一。"""

    __tablename__ = "channel_inbound_events"
    __table_args__ = (
        UniqueConstraint(
            "channel_instance_id",
            "platform_event_id",
            name="uq_channel_inbound_events_instance_event",
        ),
        Index("ix_channel_inbound_events_received_at", "received_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    platform_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ChannelDeliveryORM(Base):
    """出站投递记录：排障与"不确定投递"追溯专用，不参与业务判断。"""

    __tablename__ = "channel_deliveries"
    __table_args__ = (Index("ix_channel_deliveries_created_at", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 以下三列引用均**不设外键**（见文件头说明 2）
    channel_instance_id: Mapped[str] = mapped_column(String(36), nullable=False)
    route_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # 由 SessionRouter 分配的提交标识（不是外键引用）
    turn_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    certainty: Mapped[str] = mapped_column(String(16), nullable=False)
    segment_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    platform_message_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class ChannelAccessPolicyORM(Base):
    """准入策略：渠道实例 + 聊天类型 唯一确定一条策略（数据库中的业务数据）。"""

    __tablename__ = "channel_access_policies"
    __table_args__ = (
        UniqueConstraint(
            "channel_instance_id",
            "conversation_type",
            name="uq_channel_access_policies_instance_conversation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    channel_instance_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("channel_instances.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # open / allowlist（MVP 默认 open）
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    allowlist: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # 命令准入层：已建模，MVP 不参与判断（特性设计文档 6.1）
    allow_commands: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

