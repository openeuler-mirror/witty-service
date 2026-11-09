from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field

from witty_service.api.contract.dto.common import UtcDatetime

SESSION_EVENT_TYPES: Final[tuple[str, ...]] = (
    "turn.updated",
    "message.delta",
    "message.completed",
    "thinking.delta",
    "tool.call.started",
    "tool.call.delta",
    "tool.call.response",
    "artifact.started",
    "artifact.delta",
    "artifact.completed",
    "runtime.status",
    "resync_required",
)


class SessionEvent(BaseModel):
    """SSE 事件信封（`GET /api/sessions/{id}/events`）。

    帧格式固定为 `data: <SessionEvent JSON>\\n\\n`，不使用 SSE 的 `id:`、`event:`
    或 `retry:`，也不支持 `Last-Event-ID`。断线后服务端发送 `resync_required`，
    客户端全量重读回合与历史。客户端用 fetch 读取并携带 Bearer，不支持原生
    `EventSource`。

    P0 不提供 HITL，不产生 `question.*` 事件；旧 Console/Channel 的提问入口随
    U03/U09 删除。payload 的具体形状由 C04 定义，见 `x-payload-schema`。
    """

    model_config = ConfigDict(
        json_schema_extra={
            "x-event-types": list(SESSION_EVENT_TYPES),
            "x-unsupported-event-types": ["question.*"],
        }
    )

    event_id: str = Field(description="事件 ID；不是 SSE 的 id: 字段")
    type: str = Field(description="事件类型；已知类型见 x-event-types")
    session_id: str
    turn_id: str | None = None
    message_id: str | None = Field(
        default=None, description="关联的消息 ID；Delta 与完成事件有值"
    )
    occurred_at: UtcDatetime
    payload: dict[str, Any] = Field(json_schema_extra={"x-payload-schema": "C04"})
