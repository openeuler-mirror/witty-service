from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from witty_agent_server.runtimes.runtime_base import RuntimeType


class AgentStatus(StrEnum):
    CREATED = "created"
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    FAILED = "failed"


class Agent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    status: AgentStatus = AgentStatus.CREATED
    runtime_type: RuntimeType = "opencode"
    config: dict[str, Any] = Field(default_factory=dict)
