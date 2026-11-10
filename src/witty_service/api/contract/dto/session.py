from __future__ import annotations

from pydantic import BaseModel, Field

from witty_service.api.contract.dto.common import (
    AgentRef,
    ModelRef,
    PatchModel,
    RequestModel,
    RuntimeStatus,
    SessionOrigin,
    UtcDatetime,
)
from witty_service.api.contract.dto.turn import TurnSummary
from witty_service.domain.field_limits import SESSION_TITLE_MAX_LENGTH


class CreateSessionRequest(RequestModel):
    """只创建 web 来源的 Session；channel/scheduled 来源由进程内入口写入。"""

    workspace_id: str = Field(min_length=1)
    title: str | None = Field(
        default=None, min_length=1, max_length=SESSION_TITLE_MAX_LENGTH
    )
    agent_id: str | None = Field(
        default=None,
        min_length=1,
        description="省略或 null 时使用 Workspace 默认 Agent",
    )
    model_id: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "省略或 null 时使用 Workspace 默认模型；默认模型也为空时，"
            "Session 初始无模型，不会自动选择可用模型"
        ),
    )


class UpdateSessionRequest(PatchModel):
    title: str = Field(  # type: ignore[assignment]
        default=None, min_length=1, max_length=SESSION_TITLE_MAX_LENGTH
    )
    pinned: bool = Field(default=None)  # type: ignore[assignment]


class UpdateSessionSelectionRequest(PatchModel):
    """只影响后续回合的 Agent/模型选择。"""

    agent_id: str = Field(default=None, min_length=1)  # type: ignore[assignment]
    model_id: str = Field(default=None, min_length=1)  # type: ignore[assignment]


class SessionResponse(BaseModel):
    id: str
    workspace_id: str
    title: str | None = None
    pinned: bool
    origin: SessionOrigin
    scheduled_task_id: str | None = None
    current_agent: AgentRef
    current_model: ModelRef | None = Field(
        description=(
            "当前模型；null 表示系统没有可用模型（未配置或全部禁用），"
            "不是加载中，也不是用户主动清空；此时创建 Turn 返回 400 MODEL_REQUIRED"
        )
    )
    active_turn: TurnSummary | None = Field(
        default=None, description="该会话当前 running 的回合；没有则为 null"
    )
    runtime_status: RuntimeStatus
    last_activity_at: UtcDatetime
    created_at: UtcDatetime
    updated_at: UtcDatetime


class SessionListResponse(BaseModel):
    items: list[SessionResponse]
    next_cursor: str | None = Field(description="下一页游标；null 表示没有更多")
