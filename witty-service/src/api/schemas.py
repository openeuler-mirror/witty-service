from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    sandbox_type: str = Field(min_length=1)
    adapter_type: str = Field(min_length=1)
    idle_timeout_seconds: int = Field(gt=0)
    sandbox_id: str | None = None
    has_scheduled_tasks: bool = False


class SendMessageRequest(BaseModel):
    content: str = Field(min_length=1)


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
    has_scheduled_tasks: bool
    created_at: datetime
    updated_at: datetime
    default_session_id: str | None = None
    process_port: int | None = None
    skills: list[dict[str, Any]] = Field(default_factory=list)


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    agent_id: str
    status: str
    context_initialized: bool = False
    runtime_type: str | None = None
    created_at: datetime
    updated_at: datetime


class MessageEventsResponse(BaseModel):
    sandbox_type: str
    events: list[dict[str, Any]]


class CreateModelRequest(BaseModel):
    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    api_base_url: str | None = None
    description: str = ""
    enabled: bool = True
    max_tokens: int = 4096
    temperature: float = 0.7
    is_default: bool = False


class ModelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    provider: str
    api_base_url: str | None
    description: str
    enabled: bool
    max_tokens: int
    temperature: float
    is_default: bool
    created_at: datetime
    updated_at: datetime


class SessionEventItem(BaseModel):
    id: str
    session_id: str
    type: str
    source: str | None = None
    payload: dict[str, Any]
    timestamp: datetime


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


class SkillRepositorySourceType(str, Enum):
    GIT = 'git'
    LOCAL_IMPORT = 'local_import'


class SkillRepositoryRequest(BaseModel):
    source_type: SkillRepositorySourceType | None = None
    branch: str | None = None
    url: str | None = None
    local_path: str | None = None


class SkillRepositoryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    repo_id: str
    repo_name: str = Field(min_length=1, max_length=255)
    source_type: SkillRepositorySourceType
    branch: str | None = None
    url: str | None = None
    local_path: str | None = None
    skill_discover_status: str
    skill_num: int
    discovered_skills: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
