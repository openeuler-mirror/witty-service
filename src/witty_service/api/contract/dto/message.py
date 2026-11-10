from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from witty_service.api.contract.dto.common import UtcDatetime, WorkspacePath
from witty_service.api.contract.dto.turn import (
    TurnAgentAttribution,
    TurnModelAttribution,
)

MessageRole = Literal["user", "assistant"]
ToolStatus = Literal["pending", "running", "completed", "error"]


class TextMessagePart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ReasoningMessagePart(BaseModel):
    type: Literal["reasoning"] = "reasoning"
    text: str


class ToolMessagePart(BaseModel):
    type: Literal["tool"] = "tool"
    call_id: str
    name: str
    status: ToolStatus
    input: dict[str, Any] | None = None
    output: Any = None
    error: str | None = None


class FileMessagePart(BaseModel):
    type: Literal["file"] = "file"
    relative_path: WorkspacePath = Field(
        description="Workspace 内相对路径；不回传宿主绝对路径"
    )
    mime: str
    filename: str | None = None


MessagePart = Annotated[
    TextMessagePart | ReasoningMessagePart | ToolMessagePart | FileMessagePart,
    Field(discriminator="type"),
]


class MessageAttribution(BaseModel):
    turn_id: str
    turn_seq: int = Field(ge=1)
    agent: TurnAgentAttribution
    model: TurnModelAttribution


class MessageResponse(BaseModel):
    """只投影 text/reasoning/tool/file；Runtime 的 step 等未知 part 由投影层丢弃。"""

    id: str
    role: MessageRole
    attribution: MessageAttribution | None = Field(
        default=None, description="所属回合的归因；消息尚未关联回合时为 null"
    )
    parts: list[MessagePart]
    created_at: UtcDatetime


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    next_before: str | None = Field(
        description="更早一页的 before 游标；null 表示没有更早的消息"
    )
