from __future__ import annotations

from enum import Enum


class AgentStatus(str, Enum):
    creating = "creating"
    running = "running"
    paused = "paused"
    deleted = "deleted"
    error = "error"


def can_transition(from_status: AgentStatus, to_status: AgentStatus) -> bool:
    valid_transitions = {
        AgentStatus.creating: {AgentStatus.running, AgentStatus.deleted, AgentStatus.error},
        AgentStatus.running: {AgentStatus.paused, AgentStatus.deleted, AgentStatus.error},
        AgentStatus.paused: {AgentStatus.running, AgentStatus.deleted},
        AgentStatus.deleted: set(),  # 无法从 deleted 转换到其他状态
        AgentStatus.error: {AgentStatus.running, AgentStatus.deleted},
    }
    return to_status in valid_transitions.get(from_status, set())
