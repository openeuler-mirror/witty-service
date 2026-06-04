from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from witty_agent_server.runtimes.runtime_base import RuntimeType


DeploymentMode = Literal["local", "sandbox"]


class RuntimeTarget(BaseModel):
    """描述 agent 应被路由到哪个 runtime 目标。"""

    model_config = ConfigDict(extra="forbid")

    runtime_type: RuntimeType
    deployment_mode: DeploymentMode = "local"
    sandbox_id: str | None = None

    @model_validator(mode="after")
    def validate_target_constraints(self) -> "RuntimeTarget":
        """校验 deployment_mode 与 sandbox_id 组合。"""
        if self.deployment_mode == "sandbox":
            if not isinstance(self.sandbox_id, str) or not self.sandbox_id:
                raise ValueError("sandbox mode requires non-empty sandbox_id")
        elif self.sandbox_id is not None:
            raise ValueError("local mode must not provide sandbox_id")
        return self

    def stable_identity(self) -> str:
        """返回稳定 target 标识，用于兼容缓存键和元数据。"""
        identity_payload = {
            "runtime_type": self.runtime_type,
            "deployment_mode": self.deployment_mode,
            "sandbox_id": self.sandbox_id,
        }
        canonical_payload = json.dumps(
            identity_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()[:16]


class RuntimeContextConfig(BaseModel):
    """描述实例构建时需要的显式上下文配置。"""

    model_config = ConfigDict(extra="forbid")

    profile: str | None = None
    variables: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_context_config(self) -> "RuntimeContextConfig":
        try:
            json.dumps(self.variables, sort_keys=True, ensure_ascii=True)
            json.dumps(self.metadata, sort_keys=True, ensure_ascii=True)
        except TypeError as exc:
            raise ValueError("runtime context config must be JSON-serializable") from exc
        return self


class AgentBinding(BaseModel):
    """描述 agent_id 与 runtime 目标之间的绑定关系。"""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    target: RuntimeTarget
    context: RuntimeContextConfig = Field(default_factory=RuntimeContextConfig)
    config: dict[str, Any] = Field(default_factory=dict)


class BindingResolutionRequest(BaseModel):
    """描述一次 binding 解析请求。当前仅按 agent_id 路由。"""

    model_config = ConfigDict(extra="forbid")

    agent_id: str


class RuntimeInstance(BaseModel):
    """描述一个 agent 对应的真实 runtime 运行实例。"""

    model_config = ConfigDict(extra="forbid")

    agent_id: str
    runtime_type: RuntimeType
    deployment_mode: DeploymentMode
    instance_key: str | None = None
    sandbox_id: str | None = None
    profile_name: str | None = None
    state_dir: Path | None = None
    runtime_backup_dir: Path | None = None
    config_path: Path | None = None
    workspace_dir: Path | None = None
    log_dir: Path | None = None
    spec_path: Path | None = None
    port: int | None = None
