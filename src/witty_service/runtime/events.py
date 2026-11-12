from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from witty_service.runtime.contracts import RuntimeEventEnvelope


class RuntimeResyncReason(StrEnum):
    RUNTIME_RESTARTED = "runtime_restarted"
    RUNTIME_EXITED = "runtime_exited"
    EVENT_STREAM_INTERRUPTED = "event_stream_interrupted"
    SUBSCRIBER_LAGGED = "subscriber_lagged"
    RUNTIME_STOPPED = "runtime_stopped"


@dataclass(frozen=True, slots=True)
class RuntimeTurnEvent:
    """按活动 Turn 注册归因后的运行时事件；无活动注册时 ``turn_id`` 为空。"""

    workspace_id: str
    turn_id: str | None
    event: RuntimeEventEnvelope


@dataclass(frozen=True, slots=True)
class RuntimeResyncRequired:
    """事件链重建/断流后要求消费方全量重读；delta 无法通过重放恢复。"""

    workspace_id: str
    reason: RuntimeResyncReason
    last_durable_seq: int


RuntimeStreamItem = RuntimeTurnEvent | RuntimeResyncRequired
