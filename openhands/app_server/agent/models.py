"""Agent data models for Agent Middleware Service."""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class AgentStatus(str, Enum):
    """Agent lifecycle status."""
    CREATING = "CREATING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class AdapterType(str, Enum):
    """Supported agent adapter types."""
    OPENCODE = "opencode"
    OPENCLAW = "openclaw"
    CLAUDECODE = "claude-code"


class SessionStatus(str, Enum):
    """Session status."""
    ACTIVE = "ACTIVE"
    STOPPED = "STOPPED"


class EventType(str, Enum):
    """Agent event types for streaming responses."""
    THINKING = "thinking"
    MESSAGE = "message"
    TOOL_USE = "tool_use"
    DONE = "done"
    ERROR = "error"


class ModelOverride(BaseModel):
    """Model configuration override."""
    provider: str = Field(..., description="Model provider: anthropic/openai/google")
    name: str = Field(..., description="Model name")
    temperature: Optional[float] = Field(None, ge=0.0, le=1.0)
    max_tokens: Optional[int] = Field(None, gt=0)


class SandboxConfig(BaseModel):
    """Sandbox configuration."""
    type: str = Field(default="docker", description="Sandbox type: docker/e2b/opensandbox")
    timeout: int = Field(default=3600, gt=0, description="Timeout in seconds")


class CreateAgentRequest(BaseModel):
    """Request to create a new agent."""
    name: str = Field(..., min_length=1, max_length=255, description="Agent name")
    adapter_type: AdapterType = Field(..., description="Adapter type")
    template: dict = Field(..., description="Agent template configuration")
    model_override: Optional[ModelOverride] = Field(None, description="Model config override")
    sandbox_config: Optional[SandboxConfig] = Field(None, description="Sandbox configuration")
    idle_timeout: int = Field(default=300, gt=0, description="Idle timeout in seconds")


class AgentInfo(BaseModel):
    """Agent information response."""
    id: str = Field(..., description="Agent unique identifier")
    name: str = Field(..., description="Agent name")
    adapter_type: str = Field(..., description="Adapter type")
    status: AgentStatus = Field(..., description="Agent status")
    sandbox_id: str = Field(..., description="Associated sandbox ID")
    default_session_id: str = Field(..., description="Default session ID")
    has_scheduled_tasks: bool = Field(default=False, description="Has scheduled tasks")
    idle_timeout: int = Field(..., description="Idle timeout in seconds")
    created_at: datetime = Field(..., description="Creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    creation_log: list[str] = Field(
        default_factory=list,
        description="Recent provisioning steps (in-memory; poll with GET agent while status is CREATING)",
    )
    creation_error: Optional[str] = Field(
        None,
        description="Last provisioning error when status is ERROR after async create",
    )


class SessionInfo(BaseModel):
    """Session information."""
    id: str = Field(..., description="Session unique identifier")
    agent_id: str = Field(..., description="Parent agent ID")
    status: SessionStatus = Field(..., description="Session status")
    created_at: datetime = Field(..., description="Creation timestamp")


class MessageInfo(BaseModel):
    """Message entity persisted under a session."""
    id: str = Field(..., description="Message unique identifier")
    session_id: str = Field(..., description="Parent session ID")
    role: str = Field(..., description="Message role (user/assistant/system)")
    content: str = Field(..., description="Message content")
    event_type: Optional[str] = Field(None, description="Event type for streaming messages")
    payload: Optional[dict[str, Any]] = Field(None, description="Raw event payload")
    created_at: datetime = Field(..., description="Creation timestamp")


class SendMessageRequest(BaseModel):
    """Request to send a message to an agent."""
    session_id: str = Field(..., description="Target session ID")
    content: str = Field(..., min_length=1, description="Message content")
    attachments: list[str] = Field(default_factory=list, description="Attachment paths")


class AgentEvent(BaseModel):
    """Agent event for streaming responses."""
    type: EventType = Field(..., description="Event type")
    content: str = Field(default="", description="Event content")
    timestamp: datetime = Field(..., description="Event timestamp")
    name: Optional[str] = Field(None, description="Tool name (for tool_use events)")
    input: Optional[dict[str, Any]] = Field(None, description="Tool input parameters")
    tool_call_id: Optional[str] = Field(None, description="Tool call ID")


class UpdateAgentRequest(BaseModel):
    """Request to update agent configuration."""
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    model_override: Optional[ModelOverride] = None
    idle_timeout: Optional[int] = Field(None, gt=0)


class AdapterStartRequest(BaseModel):
    """Request to start adapter."""
    agent_id: str
    agent_type: str
    config: dict
    workspace_path: str
    restore: bool = False


class AdapterStartResponse(BaseModel):
    """Response from adapter start."""
    status: str
    sessions: list[dict] = Field(default_factory=list)


class AdapterStatusResponse(BaseModel):
    """Adapter status response."""
    status: str
    agent_type: str
    current_session_id: Optional[str] = None
    started_at: datetime


class ErrorDetail(BaseModel):
    """Error detail structure."""
    code: str
    message: str
    details: dict = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """Error response structure."""
    error: ErrorDetail
