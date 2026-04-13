from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=1)
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
    created_at: datetime
    updated_at: datetime


class MessageEventsResponse(BaseModel):
    sandbox_type: str
    events: list[dict[str, Any]]
