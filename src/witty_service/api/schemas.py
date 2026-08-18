from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, model_validator


def _format_utc_datetime(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat().replace("+00:00", "Z")


UtcDatetime = Annotated[
    datetime,
    PlainSerializer(_format_utc_datetime, return_type=str),
]


class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    sandbox_type: str = Field(min_length=1)
    adapter_type: str = Field(min_length=1)
    idle_timeout_seconds: int = Field(gt=0)
    sandbox_id: str | None = None
    model_id: str | None = None
    mcp_server_list: list[str] = Field(default_factory=list)


class CreateAgentHubRequest(BaseModel):
    """从 git 模板创建 agent 的请求。"""

    git_url: str = Field(min_length=1, description="远程 git 仓库地址")
    branch: str = Field(default="main", description="git 分支")
    sandbox_type: str = Field(min_length=1)
    adapter_type: str = Field(min_length=1)
    idle_timeout_seconds: int = Field(gt=0)
    sandbox_id: str | None = None
    model_id: str | None = None


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1)


class QuestionReplyRequest(BaseModel):
    request_id: str = Field(min_length=1)
    answers: list[list[str]] = Field(default_factory=list, max_length=100)


class QuestionRejectRequest(BaseModel):
    request_id: str = Field(min_length=1)


class InstallAgentSkillRequest(BaseModel):
    skill_id: str = Field(
        min_length=1,
        description="技能唯一标识；WittyHub 安装时可传远端 skill_id 用于记录元数据",
    )
    skill_name: str = Field(min_length=1, description="技能名；主要用于安装和日志定位")
    source_type: str | None = Field(
        default=None,
        description="安装来源类型；安装 WittyHub 技能时传 wittyhub",
    )
    skill_source: str | None = Field(
        default=None,
        description="技能来源仓库地址；source_type 为 wittyhub 时必填",
    )
    metadata: dict[str, Any] | None = Field(
        default=None,
        description="技能元数据；安装 WittyHub 技能时持久化到安装记录",
    )

    @model_validator(mode="after")
    def validate_wittyhub_payload(self) -> InstallAgentSkillRequest:
        if self.source_type == "wittyhub" and not (
            self.skill_source and self.skill_source.strip()
        ):
            raise ValueError("skill_source is required when source_type is wittyhub")
        return self


class UninstallAgentSkillRequest(BaseModel):
    skill_id: str = Field(min_length=1)


class AgentSkillResponse(BaseModel):
    agent_id: str
    skill_id: str
    source_type: str = Field(
        description="安装来源类型，如 builtin、git、local、clawhub、wittyhub"
    )
    repo_id: str | None
    skill_name: str
    installed_at: UtcDatetime
    relative_path: str | None = None
    metadata: dict[str, Any] | None = None
    skill_source: str | None = Field(
        default=None,
        description="技能来源仓库地址；source_type 为 wittyhub 时必填",
    )
    skill_md_url: str | None = None


class AgentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    description: str
    sandbox_type: str
    adapter_type: str
    status: str
    sandbox_id: str | None
    workspace_path: str
    idle_timeout_seconds: int
    model_id: str | None = None
    mcp_server_list: list[str] = Field(default_factory=list)
    created_at: UtcDatetime
    updated_at: UtcDatetime
    default_session_id: str | None = None
    process_port: int | None = None


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    status: str
    context_initialized: bool = False
    runtime_type: str | None = None
    title: str | None = None
    pinned: bool = False
    created_at: UtcDatetime
    updated_at: UtcDatetime


class MessageEventsResponse(BaseModel):
    sandbox_type: str
    events: list[dict[str, Any]]


class CreateModelRequest(BaseModel):
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    api_base_url: str | None = None
    compatibility: str | None = None  # 暂时移除正则验证，便于调试
    enabled: bool = True
    max_tokens: int = 4096
    temperature: float = 0.7
    is_default: bool = False


class UpdateModelRequest(BaseModel):
    name: str | None = Field(None, min_length=1)
    provider: str | None = Field(None, min_length=1)
    api_key: str | None = Field(None, min_length=1)
    api_base_url: str | None = None
    compatibility: str | None = None  # 暂时移除正则验证，便于调试
    enabled: bool | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    is_default: bool | None = None


class ModelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    provider: str
    api_base_url: str | None
    compatibility: str | None
    enabled: bool
    max_tokens: int
    temperature: float
    is_default: bool
    created_at: UtcDatetime
    updated_at: UtcDatetime


class CreateMcpServerRequest(BaseModel):
    mcp_server_config: dict[str, Any]


class UpdateMcpServerRequest(BaseModel):
    mcp_server_name: str | None = Field(None, min_length=1)
    mcp_server_config: dict[str, Any] | None = None


class McpServerResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    mcp_server_name: str
    mcp_server_config: dict[str, Any]
    created_at: UtcDatetime
    updated_at: UtcDatetime


class SessionEventItem(BaseModel):
    id: str
    session_id: str
    type: str
    source: str | None = None
    payload: dict[str, Any]
    timestamp: UtcDatetime


class PaginationInfo(BaseModel):
    offset: int
    limit: int
    total: int


class SessionEventPage(BaseModel):
    items: list[SessionEventItem]
    pagination: PaginationInfo


class SessionEventsResponse(BaseModel):
    items: list[SessionEventItem]
    pagination: PaginationInfo


class ConversationSummaryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    title: str | None = None
    pinned: bool = False
    scheduled_task_id: str | None = None
    status: str
    message_count: int = 0
    last_message_status: str | None = None
    first_message_preview: str | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class AgentWithConversationsResponse(AgentResponse):
    conversations: list[ConversationSummaryResponse] = Field(default_factory=list)


class ConversationDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    title: str | None = None
    pinned: bool = False
    scheduled_task_id: str | None = None
    status: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    has_more: bool = False
    created_at: UtcDatetime
    updated_at: UtcDatetime


class UpdateConversationRequest(BaseModel):
    title: str | None = None
    pinned: bool | None = None


class SkillSourceType:
    GIT = "git"
    LOCAL = "local"
    BUILDIN = "builtin"
    CLAWHUB = "clawhub"
    WITTYHUB = "wittyhub"


class SkillRepositoryRequest(BaseModel):
    source_type: str | None = None
    branch: str | None = None
    url: str | None = None
    local_path: str | None = None


class SkillRepositoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    repo_id: str
    repo_name: str = Field(min_length=1, max_length=255)
    source_type: str = Field(description="仓库来源类型；当前可能为 git、local、clawhub")
    branch: str | None = None
    url: str | None = None
    local_path: str | None = None
    skill_discover_status: str
    skill_num: int


class SkillResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    skill_id: str
    repo_id: str | None
    skill_name: str
    relative_path: str | None
    metadata: dict[str, Any]
    skill_source: str | None = Field(
        default=None,
        description="技能来源地址；可能为远程仓库 URL、本地路径或 runtime 来源标识",
    )
    skill_md_url: str | None
