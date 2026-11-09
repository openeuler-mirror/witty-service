from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

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

McpConnectionStatus = Literal["ready", "connecting", "degraded", "disabled"]


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


class SessionMcpActivationItem(BaseModel):
    mcp_server_id: str
    name: str
    active: bool = Field(description="本会话是否允许向后续回合暴露该 MCP 工具")
    enabled: bool = Field(description="全局目录开关；disabled 优先于本会话激活")
    connection_status: McpConnectionStatus = Field(
        description="Workspace 内该 MCP 的连接状态；只有 ready 的工具进入本轮可用集合"
    )


class SessionMcpActivationListResponse(BaseModel):
    items: list[SessionMcpActivationItem]


class McpActivationUpdate(RequestModel):
    mcp_server_id: str = Field(min_length=1)
    active: bool


class UpdateSessionMcpActivationsRequest(RequestModel):
    activations: list[McpActivationUpdate] = Field(min_length=1)

    @model_validator(mode="after")
    def _require_unique_servers(self) -> Self:
        server_ids = [item.mcp_server_id for item in self.activations]
        if len(server_ids) != len(set(server_ids)):
            raise ValueError("mcp_server_id must be unique")
        return self
