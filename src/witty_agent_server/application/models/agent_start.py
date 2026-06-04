from typing import Literal

from pydantic import BaseModel, ConfigDict
from witty_agent_server.runtimes.runtime_base import RuntimeType


class AgentStartRequest(BaseModel):
    """Agent 启动请求模型，供 router 与 service 调用链复用。"""

    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = None
    runtime_type: RuntimeType | None = None
    deployment_mode: Literal["local", "sandbox"] | None = None
