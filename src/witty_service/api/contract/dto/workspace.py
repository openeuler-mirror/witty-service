from __future__ import annotations

from pydantic import BaseModel, Field

from witty_service.api.contract.dto.common import (
    AgentRef,
    ModelRef,
    PatchModel,
    RequestModel,
    RuntimeStatus,
    SandboxType,
    UtcDatetime,
)


class CreateWorkspaceRequest(RequestModel):
    name: str = Field(min_length=1, max_length=255)
    host_directory: str = Field(
        min_length=1,
        max_length=4096,
        description=(
            "主机上已存在的绝对目录。服务端解析 realpath 后保存，创建后不可修改；"
            "该路径不属于响应契约，不在任何响应中返回。"
        ),
    )
    sandbox_type: SandboxType = "local_process"
    default_agent_id: str | None = Field(
        default=None, min_length=1, description="省略或 null 时使用未归档 Primary"
    )
    default_model_id: str | None = Field(
        default=None,
        min_length=1,
        description="省略或 null 时使用全局默认模型；没有可用模型时为空",
    )


class UpdateWorkspaceRequest(PatchModel):
    name: str = Field(default=None, min_length=1, max_length=255)  # type: ignore[assignment]
    default_agent_id: str | None = Field(
        default=None, min_length=1, description="显式 null 表示重置为未归档 Primary"
    )
    default_model_id: str | None = Field(
        default=None, min_length=1, description="显式 null 表示清空，回到全局默认模型"
    )


class WorkspaceResponse(BaseModel):
    id: str
    name: str
    sandbox_type: SandboxType
    is_default: bool
    default_agent: AgentRef
    default_model: ModelRef | None
    session_count: int
    runtime_status: RuntimeStatus
    runtime_reason: str | None = Field(
        default=None, description="runtime_status 非 ready 时的非密原因"
    )
    last_activity_at: UtcDatetime
    created_at: UtcDatetime
    updated_at: UtcDatetime


class WorkspaceListResponse(BaseModel):
    items: list[WorkspaceResponse]
    next_cursor: str | None = Field(description="下一页游标；null 表示没有更多")
