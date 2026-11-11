from __future__ import annotations

from dataclasses import dataclass

from witty_service.domain.enums import (
    AgentKind,
    AgentSourceType,
    ChannelAccessMode,
    ChannelConversationType,
    ChannelDeliveryCertainty,
    ChannelInstanceStatus,
    ChannelProvisioningStatus,
    MessageRole,
    SandboxType,
    ScheduledTaskRunStatus,
    ScheduledTaskScheduleType,
    SessionOrigin,
    SkillDiscoverStatus,
    TurnOperationStatus,
    TurnOperationType,
    TurnStatus,
)


@dataclass(frozen=True, slots=True)
class AgentDefinitionRecord:
    id: str
    kind: AgentKind
    name: str
    description: str
    system_prompt: str
    config_updated_at: int
    source_type: AgentSourceType
    source_ref: str | None
    archived_at: int | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class WorkspaceRecord:
    id: str
    name: str
    host_directory: str
    sandbox_type: SandboxType
    default_agent_id: str
    default_model_id: str | None
    is_default: int
    published_projection_hash: str | None
    session_count: int
    last_activity_at: int
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    workspace_id: str
    current_agent_id: str
    current_model_id: str | None
    runtime_session_id: str | None
    last_turn_seq: int
    title: str | None
    pinned: int
    origin: SessionOrigin
    scheduled_task_id: str | None
    last_activity_at: int
    deleted_at: int | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class TurnRecord:
    id: str
    session_id: str
    turn_seq: int
    agent_id: str
    model_id: str
    agent_name_snapshot: str
    model_name_snapshot: str
    status: TurnStatus
    error_code: str | None
    error_message: str | None
    started_at: int
    finished_at: int | None
    created_at: int


@dataclass(frozen=True, slots=True)
class TurnOperationRecord:
    id: str
    session_id: str
    target_turn_id: str
    type: TurnOperationType
    status: TurnOperationStatus
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class TurnRuntimeMessageRecord:
    runtime_message_id: str
    turn_id: str
    role: MessageRole
    ordinal: int
    created_at: int


@dataclass(frozen=True, slots=True)
class ModelRecord:
    id: str
    display_name: str
    provider_id: str
    provider_model_id: str
    compatibility: str | None
    api_base_url: str | None
    credential_ref: str | None
    enabled: int
    max_tokens: int
    temperature: float
    is_default: int
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class SkillRepoRecord:
    repo_id: str
    repo_name: str
    source_type: str
    branch: str | None
    url: str | None
    local_path: str | None
    skill_discover_status: SkillDiscoverStatus
    skill_count: int
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class SkillRecord:
    skill_id: str
    repo_id: str
    skill_name: str
    relative_path: str
    content_sha256: str
    metadata_json: dict[str, object]
    skill_source: str | None
    skill_md_url: str | None
    enabled: int
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class McpServerRecord:
    id: str
    mcp_server_name: str
    config_json: dict[str, object]
    credential_ref: str | None
    enabled: int
    secret_revision: int
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ScheduledTaskRecord:
    id: str
    name: str
    schedule_type: ScheduledTaskScheduleType
    cron_expr: str | None
    interval_seconds: int | None
    timezone: str
    content: str
    workspace_id: str
    agent_id: str
    model_id: str
    enabled: int
    paused: int
    reason: str | None
    last_run_id: str | None
    last_run_at: int | None
    last_run_status: ScheduledTaskRunStatus | None
    archived_at: int | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ScheduledTaskRunRecord:
    id: str
    task_id: str
    session_id: str | None
    turn_id: str | None
    status: ScheduledTaskRunStatus
    lease_owner: str | None
    lease_expires_at: int | None
    error: str | None
    started_at: int | None
    finished_at: int | None
    created_at: int


@dataclass(frozen=True, slots=True)
class ChannelInstanceRecord:
    id: str
    channel: str
    display_name: str | None
    owner_ref: str | None
    workspace_id: str | None
    agent_id: str | None
    model_id: str | None
    status: ChannelInstanceStatus
    paused: int
    reason: str | None
    generation: int
    config_json: dict[str, object]
    credential_ref: str | None
    credential_mask: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ChannelProvisioningRecord:
    id: str
    channel: str
    owner_ref: str | None
    workspace_id: str | None
    agent_id: str | None
    model_id: str | None
    status: ChannelProvisioningStatus
    qr_content: str | None
    poll_interval_ms: int
    expires_at: int
    state_ref: str | None
    error_code: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ChannelRouteRecord:
    id: str
    channel_instance_id: str
    conversation_type: ChannelConversationType
    platform_user_id: str
    active_session_id: str | None
    active_placeholder_ref: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class ChannelInboundEventRecord:
    id: str
    channel_instance_id: str
    platform_event_id: str
    received_at: int


@dataclass(frozen=True, slots=True)
class ChannelDeliveryRecord:
    id: str
    channel_instance_id: str
    route_id: str | None
    session_id: str | None
    turn_id: str | None
    certainty: ChannelDeliveryCertainty
    segment_index: int
    platform_message_ref: str | None
    error_code: str | None
    created_at: int


@dataclass(frozen=True, slots=True)
class ChannelAccessPolicyRecord:
    id: str
    channel_instance_id: str
    conversation_type: ChannelConversationType
    mode: ChannelAccessMode
    allowlist_json: list[object]
    allow_commands: int
    updated_at: int
