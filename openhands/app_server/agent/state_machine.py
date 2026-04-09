"""Lightweight event-driven state machine for agent lifecycle."""

from __future__ import annotations

from enum import Enum

from openhands.app_server.agent.models import AgentStatus


class AgentEvent(str, Enum):
    CREATE_REQUESTED = "CREATE_REQUESTED"
    CREATE_SUCCEEDED = "CREATE_SUCCEEDED"
    CREATE_FAILED = "CREATE_FAILED"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    RESUME_REQUESTED = "RESUME_REQUESTED"
    DELETE_REQUESTED = "DELETE_REQUESTED"
    RUNTIME_FAILED = "RUNTIME_FAILED"


_TRANSITIONS: dict[AgentStatus, dict[AgentEvent, AgentStatus]] = {
    AgentStatus.CREATING: {
        AgentEvent.CREATE_SUCCEEDED: AgentStatus.RUNNING,
        AgentEvent.CREATE_FAILED: AgentStatus.ERROR,
        AgentEvent.DELETE_REQUESTED: AgentStatus.STOPPED,
        AgentEvent.RUNTIME_FAILED: AgentStatus.ERROR,
    },
    AgentStatus.RUNNING: {
        AgentEvent.PAUSE_REQUESTED: AgentStatus.PAUSED,
        AgentEvent.DELETE_REQUESTED: AgentStatus.STOPPED,
        AgentEvent.RUNTIME_FAILED: AgentStatus.ERROR,
    },
    AgentStatus.PAUSED: {
        AgentEvent.RESUME_REQUESTED: AgentStatus.RUNNING,
        AgentEvent.DELETE_REQUESTED: AgentStatus.STOPPED,
        AgentEvent.RUNTIME_FAILED: AgentStatus.ERROR,
    },
    AgentStatus.ERROR: {
        AgentEvent.DELETE_REQUESTED: AgentStatus.STOPPED,
    },
    AgentStatus.STOPPED: {},
}


def transition(current: AgentStatus, event: AgentEvent) -> AgentStatus:
    """Resolve next status from current state + event.

    Raises:
        ValueError: if event is illegal for current state.
    """
    next_status = _TRANSITIONS.get(current, {}).get(event)
    if next_status is None:
        raise ValueError(f"Invalid transition: {current.value} --{event.value}--> ?")
    return next_status
