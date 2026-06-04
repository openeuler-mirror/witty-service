from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from witty_agent_server.runtimes.runtime_base import RuntimeType


class SessionConfigSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_type: RuntimeType
    prompt: str | None = None
    runtime_profile_hash: str | None = None
    skills: list[dict[str, Any]] = Field(default_factory=list)
    mcp: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    subagents: list[dict[str, Any]] = Field(default_factory=list)
