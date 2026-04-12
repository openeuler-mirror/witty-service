from __future__ import annotations

from typing import Any, Literal, TypedDict


class AgentStartResponse(TypedDict):
    status: str
    runtime_type: str
    config: dict[str, Any]
    already_running: bool


class AgentStopResponse(TypedDict):
    status: str
    runtime_type: str
    config: dict[str, Any]


class AgentStatusResponse(TypedDict):
    status: str
    runtime_type: str


class SessionCreateResponse(TypedDict):
    id: str
    context_initialized: bool
