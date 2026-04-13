from __future__ import annotations

from datetime import datetime
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
