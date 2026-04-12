from __future__ import annotations

from enum import Enum


class AgentStatus(str, Enum):
    creating = "creating"
    running = "running"
    paused = "paused"
    stopped = "stopped"
    error = "error"


_ALLOWED_TRANSITIONS: dict[AgentStatus, frozenset[AgentStatus]] = {
    AgentStatus.creating: frozenset({AgentStatus.running, AgentStatus.error}),
    AgentStatus.running: frozenset(
        {AgentStatus.paused, AgentStatus.stopped, AgentStatus.error}
    ),
    AgentStatus.paused: frozenset(
        {AgentStatus.running, AgentStatus.stopped, AgentStatus.error}
    ),
    AgentStatus.stopped: frozenset(),
    AgentStatus.error: frozenset({AgentStatus.running, AgentStatus.stopped}),
}


def _coerce_status(status: AgentStatus | str) -> AgentStatus | None:
    if isinstance(status, AgentStatus):
        return status

    try:
        return AgentStatus(status)
    except ValueError:
        return None


def can_transition(from_status: AgentStatus | str, to_status: AgentStatus | str) -> bool:
    from_value = _coerce_status(from_status)
    to_value = _coerce_status(to_status)
    if from_value is None or to_value is None:
        return False
    return to_value in _ALLOWED_TRANSITIONS[from_value]
