from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Protocol

from witty_service.runtime.errors import RuntimeContractValidationError


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise RuntimeContractValidationError(
            field=field_name, reason="must not be empty"
        )


def _require_text_fields(value: object, *names: str) -> None:
    for name in names:
        _require_text(getattr(value, name), name)


def _require_non_negative(value: int, field_name: str) -> None:
    if value < 0:
        raise RuntimeContractValidationError(
            field=field_name, reason="must be non-negative"
        )


def _require_relative_path(value: str, field_name: str) -> None:
    _require_text(value, field_name)
    if value.startswith(("/", "\\")) or "\\" in value:
        raise RuntimeContractValidationError(
            field=field_name, reason="must be a workspace-relative path"
        )
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        raise RuntimeContractValidationError(
            field=field_name, reason="must be a workspace-relative path"
        )
    if ".." in PurePosixPath(value).parts:
        raise RuntimeContractValidationError(
            field=field_name, reason="must not escape the workspace"
        )
    if "\x00" in value:
        raise RuntimeContractValidationError(
            field=field_name, reason="must not contain NUL"
        )


def _dataclass_payload(value: Any) -> dict[str, Any]:
    """把 wire DTO 转成 C03 契约 dict；``None`` 字段省略，枚举取字面值。"""

    result: dict[str, Any] = {}
    for item in fields(value):
        raw = getattr(value, item.name)
        if raw is None:
            continue
        if isinstance(raw, _WireValue):
            result[item.name] = raw.to_dict()
        elif isinstance(raw, StrEnum):
            result[item.name] = raw.value
        else:
            result[item.name] = raw
    return result


class _WireValue:
    """可序列化为 C03 契约 dict 的 wire 值基类。"""

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_payload(self)


@dataclass(frozen=True, slots=True)
class RuntimeWorkspace:
    workspace_id: str
    runtime_directory: str

    def __post_init__(self) -> None:
        _require_text_fields(self, "workspace_id", "runtime_directory")


@dataclass(frozen=True, slots=True)
class RuntimeConnection:
    """Manager 已启动的 Runtime endpoint；adapter 只负责绑定连接，不启动进程。"""

    workspace: RuntimeWorkspace
    base_url: str

    def __post_init__(self) -> None:
        _require_text(self.base_url, "base_url")


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    """adapter 探针结果；starting/restarting 等进程状态只属于 Manager 读模型。"""

    workspace_id: str
    ready: bool
    detail: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.workspace_id, "workspace_id")


@dataclass(frozen=True, slots=True)
class RuntimeModelRef:
    provider_id: str
    model_id: str

    def __post_init__(self) -> None:
        _require_text_fields(self, "provider_id", "model_id")


class RuntimePartType(StrEnum):
    TEXT = "text"
    REASONING = "reasoning"
    TOOL = "tool"
    FILE = "file"


class RuntimeToolStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class RuntimePartBase(_WireValue):
    """Part 的规范化基类；``type`` 对应 C03 ``MessagePart`` 的判别字段。"""

    @property
    def type(self) -> RuntimePartType:
        raise NotImplementedError

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type.value, **super().to_dict()}


@dataclass(frozen=True, slots=True)
class RuntimeTextPart(RuntimePartBase):
    text: str

    def __post_init__(self) -> None:
        _require_text(self.text, "text")

    @property
    def type(self) -> RuntimePartType:
        return RuntimePartType.TEXT


@dataclass(frozen=True, slots=True)
class RuntimeReasoningPart(RuntimePartBase):
    text: str

    def __post_init__(self) -> None:
        _require_text(self.text, "text")

    @property
    def type(self) -> RuntimePartType:
        return RuntimePartType.REASONING


@dataclass(frozen=True, slots=True)
class RuntimeToolPart(RuntimePartBase):
    call_id: str
    name: str
    status: RuntimeToolStatus
    input: dict[str, Any] | None = None
    output: Any = None
    error: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.call_id, "call_id")
        _require_text(self.name, "name")
        if not isinstance(self.status, RuntimeToolStatus):
            raise RuntimeContractValidationError(
                field="status", reason="must be a RuntimeToolStatus"
            )
        if self.error is not None:
            _require_text(self.error, "error")

    @property
    def type(self) -> RuntimePartType:
        return RuntimePartType.TOOL


@dataclass(frozen=True, slots=True)
class RuntimeFilePart(RuntimePartBase):
    """Workspace 相对路径的附件；绝对路径与 ``..`` 穿越在 seam 入口拒绝。"""

    relative_path: str
    mime: str
    filename: str | None = None

    def __post_init__(self) -> None:
        _require_relative_path(self.relative_path, "relative_path")
        _require_text(self.mime, "mime")
        if self.filename is not None:
            _require_text(self.filename, "filename")

    @property
    def type(self) -> RuntimePartType:
        return RuntimePartType.FILE


RuntimeInputPart = RuntimeTextPart | RuntimeFilePart
RuntimeMessagePart = (
    RuntimeTextPart | RuntimeReasoningPart | RuntimeToolPart | RuntimeFilePart
)


@dataclass(frozen=True, slots=True)
class TurnPermit:
    """Runtime Manager 内存中的回合许可。

    获取时必须原子完成“确认最新投影已发布 + 登记活动回合”，与配置发布互斥；
    不携带配置代次，也不持久化到数据库。
    """

    permit_id: str
    workspace_id: str
    turn_id: str
    published_projection_hash: str

    def __post_init__(self) -> None:
        for name in (
            "permit_id",
            "workspace_id",
            "turn_id",
            "published_projection_hash",
        ):
            _require_text(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class EnsureSessionRequest:
    workspace: RuntimeWorkspace
    session_id: str
    runtime_session_id: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.session_id, "session_id")
        if self.runtime_session_id is not None:
            _require_text(self.runtime_session_id, "runtime_session_id")


@dataclass(frozen=True, slots=True)
class RuntimeSession:
    workspace_id: str
    session_id: str
    runtime_session_id: str

    def __post_init__(self) -> None:
        _require_text_fields(self, "workspace_id", "session_id", "runtime_session_id")


@dataclass(frozen=True, slots=True)
class TurnExecutionRequest:
    """一次执行请求的唯一输入；目录只在 ensure session 固定。

    ``resume`` 是 v2 prompt 的三态开关，不能用 bool 默认值抹平：

    - ``None``：首次提交，adapter 必须省略 ``resume`` 字段，走默认执行路径；
    - ``True``：同一 ``message_id`` 的恢复执行，显式传 ``resume: true``；
    - ``False``：只做 admission 预占，Runtime 不会开始执行。
    """

    workspace_id: str
    session_id: str
    turn_id: str
    runtime_session_id: str
    message_id: str
    agent_internal_name: str
    model: RuntimeModelRef
    parts: tuple[RuntimeInputPart, ...]
    resume: bool | None = None

    def __post_init__(self) -> None:
        for name in (
            "workspace_id",
            "session_id",
            "turn_id",
            "runtime_session_id",
            "agent_internal_name",
        ):
            _require_text(getattr(self, name), name)
        _require_text(self.message_id, "message_id")
        if not self.parts:
            raise RuntimeContractValidationError(
                field="parts", reason="must not be empty"
            )
        for part in self.parts:
            if not isinstance(part, (RuntimeTextPart, RuntimeFilePart)):
                raise RuntimeContractValidationError(
                    field="parts", reason="only text and file parts are accepted"
                )

    @property
    def text(self) -> str:
        """发送给 Runtime 的单一文本；幂等键使用同一值。"""

        return "".join(
            part.text for part in self.parts if isinstance(part, RuntimeTextPart)
        )

    @property
    def file_parts(self) -> tuple[RuntimeFilePart, ...]:
        return tuple(part for part in self.parts if isinstance(part, RuntimeFilePart))

    @property
    def idempotency_key(
        self,
    ) -> tuple[str, str, bool | None, tuple[tuple[str, str, str | None], ...]]:
        files = tuple(
            (part.relative_path, part.mime, part.filename) for part in self.file_parts
        )
        return (self.message_id, self.text, self.resume, files)


@dataclass(frozen=True, slots=True)
class TurnAdmission:
    """Runtime 已接受 prompt；不代表执行已开始，也不代表已进入终态。"""

    message_id: str
    runtime_session_id: str
    admitted_seq: int
    delivery: str
    admitted_at: int
    promoted_seq: int | None = None

    def __post_init__(self) -> None:
        _require_text_fields(self, "message_id", "runtime_session_id", "delivery")
        _require_non_negative(self.admitted_seq, "admitted_seq")
        _require_non_negative(self.admitted_at, "admitted_at")
        if self.promoted_seq is not None:
            _require_non_negative(self.promoted_seq, "promoted_seq")


class RuntimeEventType(StrEnum):
    MESSAGE_STARTED = "message.started"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    THINKING_DELTA = "thinking.delta"
    TOOL_CALL_STARTED = "tool.call.started"
    TOOL_CALL_DELTA = "tool.call.delta"
    TOOL_CALL_RESPONSE = "tool.call.response"
    ARTIFACT_STARTED = "artifact.started"
    ARTIFACT_DELTA = "artifact.delta"
    ARTIFACT_COMPLETED = "artifact.completed"
    SESSION_USAGE = "session.usage"
    # 只映射 v2 step.started；admission 与 session.next.prompted 都不算执行开始。
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    TURN_FAILED = "turn.failed"
    SESSION_ERROR = "session.error"
    SESSION_IDLE = "session.idle"


@dataclass(frozen=True, slots=True)
class RuntimeEmptyPayload(_WireValue):
    """无载荷事件（started / idle / turn.completed）。"""


@dataclass(frozen=True, slots=True)
class RuntimeMessageDeltaPayload(_WireValue):
    delta: str


@dataclass(frozen=True, slots=True)
class RuntimeMessageCompletedPayload(_WireValue):
    finish: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeThinkingDeltaPayload(_WireValue):
    delta: str


@dataclass(frozen=True, slots=True)
class RuntimeToolCallStartedPayload(_WireValue):
    tool_call_id: str
    tool_name: str
    input: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_text(self.tool_call_id, "tool_call_id")
        _require_text(self.tool_name, "tool_name")


@dataclass(frozen=True, slots=True)
class RuntimeToolCallDeltaPayload(_WireValue):
    tool_call_id: str
    tool_name: str
    delta: str

    def __post_init__(self) -> None:
        _require_text(self.tool_call_id, "tool_call_id")
        _require_text(self.tool_name, "tool_name")


@dataclass(frozen=True, slots=True)
class RuntimeToolCallResponsePayload(_WireValue):
    tool_call_id: str
    tool_name: str
    output: Any = None
    error: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.tool_call_id, "tool_call_id")
        _require_text(self.tool_name, "tool_name")
        if self.error is not None:
            _require_text(self.error, "error")


@dataclass(frozen=True, slots=True)
class RuntimeArtifactPayload(_WireValue):
    """与 ``artifact_detector`` 产出的载荷保持一致。"""

    id: str
    name: str
    type: str
    status: str
    relative_path: str
    mime: str
    version: int = 1
    size: int | None = None
    content: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "name", "type", "status", "relative_path", "mime"):
            _require_text(getattr(self, name), name)
        if self.size is not None:
            _require_non_negative(self.size, "size")


@dataclass(frozen=True, slots=True)
class RuntimeUsagePayload(_WireValue):
    """单轮累计用量；字段口径见 ``witty_service.runtime.usage``。"""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    total_cost: float | None = None


@dataclass(frozen=True, slots=True)
class RuntimeErrorPayload(_WireValue):
    code: str
    message: str
    source: str = "runtime"
    retryable: bool = False
    detail: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _require_text_fields(self, "code", "message", "source")


RuntimeEventPayload = (
    RuntimeEmptyPayload
    | RuntimeMessageDeltaPayload
    | RuntimeMessageCompletedPayload
    | RuntimeThinkingDeltaPayload
    | RuntimeToolCallStartedPayload
    | RuntimeToolCallDeltaPayload
    | RuntimeToolCallResponsePayload
    | RuntimeArtifactPayload
    | RuntimeUsagePayload
    | RuntimeErrorPayload
)

RUNTIME_EVENT_PAYLOAD_TYPES: dict[RuntimeEventType, type[_WireValue]] = {
    RuntimeEventType.MESSAGE_STARTED: RuntimeEmptyPayload,
    RuntimeEventType.MESSAGE_DELTA: RuntimeMessageDeltaPayload,
    RuntimeEventType.MESSAGE_COMPLETED: RuntimeMessageCompletedPayload,
    RuntimeEventType.THINKING_DELTA: RuntimeThinkingDeltaPayload,
    RuntimeEventType.TOOL_CALL_STARTED: RuntimeToolCallStartedPayload,
    RuntimeEventType.TOOL_CALL_DELTA: RuntimeToolCallDeltaPayload,
    RuntimeEventType.TOOL_CALL_RESPONSE: RuntimeToolCallResponsePayload,
    RuntimeEventType.ARTIFACT_STARTED: RuntimeArtifactPayload,
    RuntimeEventType.ARTIFACT_DELTA: RuntimeArtifactPayload,
    RuntimeEventType.ARTIFACT_COMPLETED: RuntimeArtifactPayload,
    RuntimeEventType.SESSION_USAGE: RuntimeUsagePayload,
    RuntimeEventType.TURN_STARTED: RuntimeEmptyPayload,
    RuntimeEventType.TURN_COMPLETED: RuntimeEmptyPayload,
    RuntimeEventType.TURN_FAILED: RuntimeErrorPayload,
    RuntimeEventType.SESSION_ERROR: RuntimeErrorPayload,
    RuntimeEventType.SESSION_IDLE: RuntimeEmptyPayload,
}


@dataclass(frozen=True, slots=True)
class RuntimeEventEnvelope:
    """``durable_seq`` 是重放与去重键；``event_id`` 只用于诊断。

    ``payload`` 必须是事件类型对应的规范化 DTO，adapter 不得透传上游原始 dict；
    Manager 通过 ``payload.to_dict()`` 生成 C03 事件载荷。
    """

    workspace_id: str
    runtime_session_id: str
    event_id: str
    type: RuntimeEventType
    durable_seq: int
    occurred_at: int
    payload: RuntimeEventPayload
    runtime_message_id: str | None = None

    def __post_init__(self) -> None:
        _require_text_fields(self, "workspace_id", "runtime_session_id", "event_id")
        _require_non_negative(self.durable_seq, "durable_seq")
        _require_non_negative(self.occurred_at, "occurred_at")
        if self.runtime_message_id is not None:
            _require_text(self.runtime_message_id, "runtime_message_id")
        if not isinstance(self.type, RuntimeEventType):
            raise RuntimeContractValidationError(
                field="type", reason="must be a RuntimeEventType"
            )
        expected = RUNTIME_EVENT_PAYLOAD_TYPES.get(self.type)
        if expected is None:
            raise RuntimeContractValidationError(
                field="type", reason="unknown runtime event type"
            )
        if not isinstance(self.payload, expected):
            raise RuntimeContractValidationError(
                field="payload",
                reason=f"payload does not match event type {self.type.value}",
            )


@dataclass(frozen=True, slots=True)
class ReplayQuery:
    workspace_id: str
    runtime_session_id: str
    after_seq: int = 0
    limit: int = 100

    def __post_init__(self) -> None:
        _require_text_fields(self, "workspace_id", "runtime_session_id")
        _require_non_negative(self.after_seq, "after_seq")
        if self.limit < 1:
            raise RuntimeContractValidationError(
                field="limit", reason="must be positive"
            )


@dataclass(frozen=True, slots=True)
class RuntimeEventReplay:
    """``events`` 按 ``durable_seq`` 升序返回。"""

    events: tuple[RuntimeEventEnvelope, ...]
    has_more: bool


class RuntimeMessageKind(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    AGENT_SWITCHED = "agent_switched"
    MODEL_SWITCHED = "model_switched"


@dataclass(frozen=True, slots=True)
class RuntimeHistoryMessage:
    message_id: str
    kind: RuntimeMessageKind
    created_at: int
    parts: tuple[RuntimeMessagePart, ...] = ()
    finish: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.message_id, "message_id")
        _require_non_negative(self.created_at, "created_at")
        for part in self.parts:
            if not isinstance(
                part,
                (
                    RuntimeTextPart,
                    RuntimeReasoningPart,
                    RuntimeToolPart,
                    RuntimeFilePart,
                ),
            ):
                raise RuntimeContractValidationError(
                    field="parts", reason="unsupported message part"
                )


@dataclass(frozen=True, slots=True)
class HistoryQuery:
    workspace_id: str
    runtime_session_id: str
    limit: int = 50
    cursor: str | None = None

    def __post_init__(self) -> None:
        _require_text_fields(self, "workspace_id", "runtime_session_id")
        if self.limit < 1:
            raise RuntimeContractValidationError(
                field="limit", reason="must be positive"
            )
        if self.cursor is not None:
            _require_text(self.cursor, "cursor")


@dataclass(frozen=True, slots=True)
class HistoryPage:
    """消息最新在前；``next_cursor`` 翻更旧，``previous_cursor`` 翻更新。"""

    items: tuple[RuntimeHistoryMessage, ...]
    next_cursor: str | None = None
    previous_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeRevertResult:
    runtime_message_id: str
    diff: str
    files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.runtime_message_id, "runtime_message_id")


@dataclass(frozen=True, slots=True)
class RuntimeLocationReadiness:
    """目标 location 的可见性与可执行性；进程健康与全局 catalog 不算就绪。"""

    workspace_id: str
    ready: bool
    agents: tuple[str, ...] = ()
    models: tuple[RuntimeModelRef, ...] = ()
    skills: tuple[str, ...] = ()
    detail: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.workspace_id, "workspace_id")


@dataclass(frozen=True, slots=True)
class RuntimeSessionState:
    runtime_session_id: str
    running: bool
    revert_target_message_id: str | None = None
    last_message_id: str | None = None
    last_durable_seq: int = 0

    def __post_init__(self) -> None:
        _require_text(self.runtime_session_id, "runtime_session_id")
        _require_non_negative(self.last_durable_seq, "last_durable_seq")


@dataclass(frozen=True, slots=True)
class RuntimeWorkspaceSnapshot:
    workspace_id: str
    health: RuntimeHealth
    location: RuntimeLocationReadiness
    active_runtime_session_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.workspace_id, "workspace_id")
        if not (
            self.workspace_id == self.health.workspace_id == self.location.workspace_id
        ):
            raise RuntimeContractValidationError(
                field="workspace_id",
                reason="snapshot parts must describe the same workspace",
            )


class WorkspaceRuntimePort(Protocol):
    """控制面与 OpenCode v2 执行适配层之间的唯一 seam。"""

    async def connect(self, connection: RuntimeConnection) -> RuntimeHealth: ...

    async def disconnect(self, workspace_id: str) -> None: ...

    async def health(self, workspace_id: str) -> RuntimeHealth: ...

    async def inspect_workspace(
        self, workspace_id: str
    ) -> RuntimeWorkspaceSnapshot: ...

    async def ensure_session(self, request: EnsureSessionRequest) -> RuntimeSession: ...

    async def prepare_turn(self, request: TurnExecutionRequest) -> None: ...

    async def admit_turn(self, request: TurnExecutionRequest) -> TurnAdmission: ...

    def subscribe(self, workspace_id: str) -> AsyncIterator[RuntimeEventEnvelope]: ...

    async def replay_events(self, query: ReplayQuery) -> RuntimeEventReplay: ...

    async def read_history(self, query: HistoryQuery) -> HistoryPage: ...

    async def interrupt_session(
        self, *, workspace_id: str, runtime_session_id: str
    ) -> None: ...

    async def stage_revert(
        self,
        *,
        workspace_id: str,
        runtime_session_id: str,
        runtime_message_id: str,
    ) -> RuntimeRevertResult: ...

    async def commit_revert(
        self, *, workspace_id: str, runtime_session_id: str
    ) -> None: ...

    async def clear_revert(
        self, *, workspace_id: str, runtime_session_id: str
    ) -> None: ...

    async def delete_session(
        self, *, workspace_id: str, runtime_session_id: str
    ) -> None: ...

    async def inspect_session(
        self, *, workspace_id: str, runtime_session_id: str
    ) -> RuntimeSessionState: ...
