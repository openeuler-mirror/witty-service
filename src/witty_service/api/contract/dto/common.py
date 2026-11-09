from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    WithJsonSchema,
    model_validator,
)

AgentKind = Literal["primary", "expert"]
SandboxType = Literal["local", "docker"]
SessionOrigin = Literal["web", "channel", "scheduled"]
RuntimeStatus = Annotated[
    Literal["stopped", "starting", "ready", "restarting", "unavailable"],
    Field(
        description=(
            "Runtime 可用性；ready 对应 ADR 的 healthy，不是完整进程状态机；"
            "非 ready 时历史与产物读取返回 503"
        )
    ),
]


def _utc_isoformat(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


UtcDatetime = Annotated[
    AwareDatetime,
    PlainSerializer(_utc_isoformat, return_type=str),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]


def _workspace_relative_path(value: str) -> str:
    if not value:
        raise ValueError("path must not be empty")
    if value.startswith(("/", "\\")) or "\\" in value:
        raise ValueError("path must be relative to the workspace")
    if value[1:2] == ":" and value[2:3] == "/" and value[:1].isalpha():
        raise ValueError("path must be relative to the workspace")
    if ".." in PurePosixPath(value).parts:
        raise ValueError("path must not escape the workspace")
    if "\x00" in value:
        raise ValueError("path must not contain NUL")
    return value


WorkspacePath = Annotated[str, AfterValidator(_workspace_relative_path)]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PatchModel(RequestModel):
    """PATCH 语义：省略字段表示不修改；空更新非法。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"minProperties": 1},
    )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: Any, handler: Any
    ) -> dict[str, Any]:
        schema: dict[str, Any] = handler(core_schema)
        for prop in schema.get("properties", {}).values():
            if prop.get("default", ...) is None:
                prop.pop("default", None)
        return schema

    @model_validator(mode="after")
    def _require_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class AgentRef(BaseModel):
    id: str
    name: str
    kind: AgentKind
    archived: bool = Field(
        description="Agent 已归档；已有 Session 仍保留该选择，但新 Turn 返回 409"
    )


class ModelRef(BaseModel):
    id: str
    display_name: str
    provider_id: str
    provider_model_id: str
    enabled: bool = Field(
        description="模型已启用；已有 Session 仍保留该选择，但新 Turn 返回 409"
    )
