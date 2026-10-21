from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentCommonConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_name: str
    soul: str
    prompt: str
    skills: list[dict[str, Any]] = Field(default_factory=list)
    mcp: dict[str, Any] = Field(default_factory=dict)
    tools: list[dict[str, Any]] = Field(default_factory=list)
    subagents: list[dict[str, Any]] = Field(default_factory=list)


class AgentRuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    common_config: AgentCommonConfig | None = None
    runtime_config: AgentRuntimeConfig | None = None
