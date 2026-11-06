from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class RuntimeWorkspace:
    workspace_id: str
    runtime_directory: str

    def __post_init__(self) -> None:
        _require_text(self.workspace_id, "workspace_id")
        _require_text(self.runtime_directory, "runtime_directory")


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    workspace_id: str
    status: Literal["ready", "stopped", "unavailable"]
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeLease:
    lease_id: str
    workspace_id: str
    config_generation: int
    projection_hash: str

    def __post_init__(self) -> None:
        _require_text(self.lease_id, "lease_id")
        _require_text(self.workspace_id, "workspace_id")
        _require_text(self.projection_hash, "projection_hash")
        if self.config_generation < 0:
            raise ValueError("config_generation must be non-negative")


@dataclass(frozen=True, slots=True)
class RuntimeSession:
    workspace_id: str
    session_id: str
    runtime_session_id: str


@dataclass(frozen=True, slots=True)
class RuntimePart:
    type: Literal["text", "file"]
    text: str | None = None
    url: str | None = None
    mime: str | None = None
    filename: str | None = None

    def __post_init__(self) -> None:
        if self.type == "text" and not self.text:
            raise ValueError("text part requires text")
        if self.type == "file" and (not self.url or not self.mime):
            raise ValueError("file part requires url and mime")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.type}
        for name in ("text", "url", "mime", "filename"):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        return result


@dataclass(frozen=True, slots=True)
class TurnExecutionRequest:
    workspace_id: str
    session_id: str
    turn_id: str
    runtime_session_id: str
    agent_internal_name: str
    provider_id: str
    model_id: str
    parts: tuple[RuntimePart, ...]
    config_generation: int
    projection_hash: str

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "session_id",
            "turn_id",
            "runtime_session_id",
            "agent_internal_name",
            "provider_id",
            "model_id",
            "projection_hash",
        ):
            _require_text(getattr(self, name), name)
        if not self.parts:
            raise ValueError("parts must not be empty")
        if self.config_generation < 0:
            raise ValueError("config_generation must be non-negative")


class RuntimeEventType(StrEnum):
    MESSAGE_STARTED = "message.started"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    TURN_COMPLETED = "turn.completed"
    THINKING = "thinking"
    THINKING_DELTA = "thinking.delta"
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_DELTA = "tool.call.delta"
    TOOL_CALL_RESPONSE = "tool.call.response"
    ARTIFACT_STARTED = "artifact.started"
    ARTIFACT_DELTA = "artifact.delta"
    ARTIFACT_COMPLETED = "artifact.completed"
    SESSION_USAGE = "session.usage"
    STREAM_ERROR = "stream.error"
    QUESTION_ASKED = "question.asked"
    QUESTION_REPLIED = "question.replied"
    QUESTION_REJECTED = "question.rejected"


@dataclass(frozen=True, slots=True)
class RuntimeEventEnvelope:
    workspace_id: str
    runtime_session_id: str
    event_id: str
    type: RuntimeEventType
    occurred_at: int
    payload: dict[str, Any] = field(default_factory=dict)
    runtime_message_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.workspace_id, "workspace_id")
        _require_text(self.runtime_session_id, "runtime_session_id")
        _require_text(self.event_id, "event_id")
        if self.occurred_at < 0:
            raise ValueError("occurred_at must be Unix epoch milliseconds")


@dataclass(frozen=True, slots=True)
class RuntimeHistoryMessage:
    runtime_message_id: str
    role: Literal["user", "assistant"]
    parts: tuple[dict[str, Any], ...]
    created_at: int


@dataclass(frozen=True, slots=True)
class HistoryPage:
    items: tuple[RuntimeHistoryMessage, ...]
    next_cursor: str | None


class WorkspaceRuntimePort(Protocol):
    async def ensure_ready(self, workspace: RuntimeWorkspace) -> RuntimeHealth: ...

    async def health(self, workspace_id: str) -> RuntimeHealth: ...

    async def stop(self, workspace_id: str) -> None: ...

    async def acquire_lease(
        self, workspace_id: str, *, projection_hash: str
    ) -> RuntimeLease: ...

    async def release_lease(self, lease: RuntimeLease) -> None: ...

    async def create_session(
        self, *, session_id: str, lease: RuntimeLease
    ) -> RuntimeSession: ...

    def execute_turn(
        self, request: TurnExecutionRequest, *, lease: RuntimeLease
    ) -> AsyncIterator[RuntimeEventEnvelope]: ...

    async def abort_turn(
        self, *, runtime_session_id: str, lease: RuntimeLease
    ) -> None: ...

    async def revert_turn(
        self,
        *,
        runtime_session_id: str,
        runtime_message_id: str,
        lease: RuntimeLease,
    ) -> None: ...

    async def delete_session(
        self, *, workspace_id: str, runtime_session_id: str
    ) -> None: ...

    def subscribe(self, workspace_id: str) -> AsyncIterator[RuntimeEventEnvelope]: ...

    async def read_history(
        self,
        *,
        workspace_id: str,
        runtime_session_id: str,
        limit: int,
        before: str | None = None,
    ) -> HistoryPage: ...
