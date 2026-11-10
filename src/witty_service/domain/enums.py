from __future__ import annotations

from enum import StrEnum


class AgentStatus(StrEnum):
    creating = "creating"
    running = "running"
    paused = "paused"
    deleted = "deleted"
    error = "error"


class AgentKind(StrEnum):
    primary = "primary"
    expert = "expert"


class AgentSourceType(StrEnum):
    local = "local"
    market = "market"
    preset = "preset"


class SandboxType(StrEnum):
    local = "local"
    docker = "docker"


class SessionOrigin(StrEnum):
    web = "web"
    channel = "channel"
    scheduled = "scheduled"


class TurnStatus(StrEnum):
    running = "running"
    completed = "completed"
    error = "error"
    interrupted = "interrupted"


class TurnOperationType(StrEnum):
    regenerate = "regenerate"


class TurnOperationStatus(StrEnum):
    reverting = "reverting"
    accepted = "accepted"
    failed = "failed"
    interrupted = "interrupted"


class MessageRole(StrEnum):
    user = "user"
    assistant = "assistant"


class SkillDiscoverStatus(StrEnum):
    init = "init"
    discovering = "discovering"
    ready = "ready"
    error = "error"


class ScheduledTaskScheduleType(StrEnum):
    cron = "cron"
    interval = "interval"


class ScheduledTaskRunStatus(StrEnum):
    """定时任务单次运行记录的状态。"""

    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    skipped = "skipped"


class ChannelInstanceStatus(StrEnum):
    pending = "pending"
    connected = "connected"
    degraded = "degraded"
    offline = "offline"
    error = "error"
    disabled = "disabled"


class ChannelProvisioningStatus(StrEnum):
    waiting = "waiting"
    succeeded = "succeeded"
    expired = "expired"
    failed = "failed"
    cancelled = "cancelled"


class ChannelConversationType(StrEnum):
    direct = "direct"
    group = "group"


class ChannelDeliveryCertainty(StrEnum):
    delivered = "delivered"
    rejected = "rejected"
    uncertain = "uncertain"


class ChannelAccessMode(StrEnum):
    open = "open"
    allowlist = "allowlist"


def can_transition(from_status: AgentStatus, to_status: AgentStatus) -> bool:
    valid_transitions = {
        AgentStatus.creating: {
            AgentStatus.running,
            AgentStatus.deleted,
            AgentStatus.error,
        },
        AgentStatus.running: {
            AgentStatus.paused,
            AgentStatus.deleted,
            AgentStatus.error,
        },
        AgentStatus.paused: {AgentStatus.running, AgentStatus.deleted},
        AgentStatus.deleted: set(),  # 无法从 deleted 转换到其他状态
        AgentStatus.error: {AgentStatus.running, AgentStatus.deleted},
    }
    return to_status in valid_transitions.get(from_status, set())
