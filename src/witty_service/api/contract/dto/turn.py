from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from witty_service.api.contract.dto.common import (
    RequestModel,
    UtcDatetime,
    WorkspacePath,
)

TurnStatus = Literal["running", "completed", "error", "interrupted"]


class TextInputPart(RequestModel):
    type: Literal["text"]
    text: str = Field(min_length=1)


class FileInputPart(RequestModel):
    type: Literal["file"]
    relative_path: WorkspacePath = Field(
        description="Workspace 内相对路径；绝对路径与 .. 逃逸非法"
    )
    mime: str = Field(min_length=1)
    filename: str | None = None


InputPart = Annotated[TextInputPart | FileInputPart, Field(discriminator="type")]


class CreateTurnRequest(RequestModel):
    parts: list[InputPart] = Field(min_length=1)


class TurnAgentAttribution(BaseModel):
    id: str
    name: str = Field(description="回合准入时固定的 Agent 展示名")


class TurnModelAttribution(BaseModel):
    id: str
    display_name: str = Field(description="回合准入时固定的模型展示名")


class TurnError(BaseModel):
    code: str
    message: str


class TurnSummary(BaseModel):
    id: str
    turn_seq: int = Field(ge=1)
    status: TurnStatus


class TurnResponse(BaseModel):
    id: str
    session_id: str
    turn_seq: int = Field(ge=1)
    status: TurnStatus
    agent: TurnAgentAttribution
    model: TurnModelAttribution
    error: TurnError | None = None
    started_at: UtcDatetime
    finished_at: UtcDatetime | None = None
    created_at: UtcDatetime


class TurnListResponse(BaseModel):
    items: list[TurnResponse]
    next_cursor: str | None = Field(description="下一页游标；null 表示没有更多")


class AbortTurnResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "turn": {
                        "id": "trn_02",
                        "session_id": "ses_01",
                        "turn_seq": 3,
                        "status": "interrupted",
                        "agent": {"id": "agt_01", "name": "Primary"},
                        "model": {"id": "mdl_01", "display_name": "DeepSeek V3"},
                        "started_at": "2026-11-09T07:30:00.000Z",
                        "finished_at": "2026-11-09T07:30:05.000Z",
                        "created_at": "2026-11-09T07:30:00.000Z",
                    },
                    "already_stopped": True,
                }
            ]
        }
    )

    turn: TurnResponse
    already_stopped: bool = Field(
        description=(
            "true 表示该回合已处于终态，本次 abort 未产生副作用；重复 abort 仍返回 200"
        )
    )
