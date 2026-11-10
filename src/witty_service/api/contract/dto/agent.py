from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from witty_service.api.contract.dto.common import AgentKind, RequestModel, UtcDatetime
from witty_service.domain.field_limits import (
    AGENT_DESCRIPTION_MAX_LENGTH,
    AGENT_NAME_MAX_LENGTH,
)

AgentSourceType = Literal["local", "market", "preset"]


class AgentSummary(BaseModel):
    id: str
    kind: AgentKind
    name: str
    description: str
    source_type: AgentSourceType
    source_ref: str | None = None
    archived_at: UtcDatetime | None = None
    config_updated_at: UtcDatetime
    created_at: UtcDatetime
    updated_at: UtcDatetime


class AgentResponse(AgentSummary):
    system_prompt: str


class AgentListResponse(BaseModel):
    items: list[AgentSummary]
    next_cursor: str | None = Field(description="下一页游标；null 表示没有更多")


class CreateAgentRequest(RequestModel):
    """创建 Expert Agent Definition；Primary 由系统种子创建。

    来源（local/market/preset 及引用）是服务端安装事实，由服务端写入，不接受
    客户端指定。
    """

    name: str = Field(min_length=1, max_length=AGENT_NAME_MAX_LENGTH)
    description: str = Field(default="", max_length=AGENT_DESCRIPTION_MAX_LENGTH)
    system_prompt: str = ""


class UpdateAgentRequest(RequestModel):
    """整份编辑定义字段；来源是创建事实，编辑不改变来源。"""

    name: str = Field(min_length=1, max_length=AGENT_NAME_MAX_LENGTH)
    description: str = Field(max_length=AGENT_DESCRIPTION_MAX_LENGTH)
    system_prompt: str
    expected_config_updated_at: UtcDatetime = Field(
        description="读取时的 config_updated_at；不一致返回 409 CONFIG_UPDATE_CONFLICT"
    )
