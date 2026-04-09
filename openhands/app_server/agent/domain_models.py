"""Domain models for agent runtime."""

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from openhands.app_server.agent.models import AgentInfo, AgentStatus, MessageInfo, SessionInfo, SessionStatus


class Agent(BaseModel):
    id: str
    name: str
    adapter_type: str
    status: AgentStatus
    sandbox_id: str = ""
    default_session_id: str = ""
    has_scheduled_tasks: bool = False
    idle_timeout: int = 300
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    creation_log: list[str] = Field(default_factory=list)
    creation_error: Optional[str] = None

    def to_info(self) -> AgentInfo:
        return AgentInfo(**self.model_dump())


class Session(BaseModel):
    id: str
    agent_id: str
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: datetime = Field(default_factory=datetime.now)

    def to_info(self) -> SessionInfo:
        return SessionInfo(**self.model_dump())


class Message(BaseModel):
    id: str
    session_id: str
    role: str
    content: str
    event_type: Optional[str] = None
    payload: Optional[dict[str, Any]] = None
    created_at: datetime = Field(default_factory=datetime.now)

    def to_info(self) -> MessageInfo:
        return MessageInfo(**self.model_dump())
