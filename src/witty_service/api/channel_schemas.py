"""IM Channel REST 接口的请求 / 响应模型（框架设计 §9）。

三条硬性约定：

- **永不回传凭据**：所有响应只有 `credential_mask`；`config` 只含适配器显式声明的
  非密字段（`config_fields`），密文字段一律不出现；
- **平台侧临时凭据只存在于服务端**：接入响应里只有 `qr_content` 与状态；
- **agent 状态区分"未绑定"与"已删除"**：`agent_state` 取 `unbound` / `deleted` /
  agent 的实际状态值，`agent_name` 只在可用时给出。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from witty_service.channels.access_policy import MODE_OPEN
from witty_service.channels.contracts import CONVERSATION_TYPE_DIRECT
from witty_service.persistence.channel_repository import (
    ChannelAccessPolicyRecord,
    ChannelInstanceRecord,
)


class ChannelCapabilitiesResponse(BaseModel):
    """能力声明（未连接时为保守值）。"""

    can_edit_message: bool
    max_text_length: int
    max_reply_segments: int | None = None


class ChannelCredentialFieldResponse(BaseModel):
    """手填凭据表单的字段描述：由适配器声明驱动，前端不硬编码字段名。"""

    name: str
    label: str
    secret: bool
    required: bool


class ChannelCatalogItemResponse(BaseModel):
    """`GET /channels/catalog` 的一项：调用方据此渲染渠道选择与手填表单。"""

    channel: str
    display_name: str
    adapter_version: str
    supports_provisioning: bool
    credential_fields: list[ChannelCredentialFieldResponse]
    config_fields: list[str]
    capabilities: ChannelCapabilitiesResponse


class ChannelInstanceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    channel: str
    display_name: str | None = None
    owner_ref: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    #: unbound | deleted | agent 的实际状态值
    agent_state: str
    status: str
    credential_mask: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    generation: int
    connected: bool = False
    created_at: datetime
    updated_at: datetime


class ProvisionBeginRequest(BaseModel):
    channel: str
    owner_ref: str | None = None
    agent_id: str | None = None


class ProvisionResponse(BaseModel):
    attempt_id: str
    channel: str
    status: str
    qr_content: str | None = None
    expires_at: datetime
    poll_interval_ms: int
    error_code: str | None = None
    instance: ChannelInstanceResponse | None = None


class CreateChannelInstanceRequest(BaseModel):
    """手填凭据旁路（框架设计 §8.1 的救援通道）。"""

    channel: str
    credentials: dict[str, str]
    owner_ref: str | None = None
    agent_id: str | None = None
    display_name: str | None = None


class UpdateChannelInstanceRequest(BaseModel):
    agent_id: str | None = None
    display_name: str | None = None


class ChannelTestRequest(BaseModel):
    platform_user_id: str
    conversation_type: str = CONVERSATION_TYPE_DIRECT


class ChannelTestResponse(BaseModel):
    """投递三态；`rejected` 时 `error_code` 必有值。"""

    certainty: str
    error_code: str | None = None
    platform_message_ref: str | None = None


class AccessPolicyEntrySchema(BaseModel):
    mode: str = MODE_OPEN
    allowlist: list[str] = Field(default_factory=list)
    allow_commands: bool = True


class ChannelAccessPolicyResponse(BaseModel):
    direct: AccessPolicyEntrySchema
    group: AccessPolicyEntrySchema


class UpdateChannelAccessPolicyRequest(BaseModel):
    """部分更新：只写调用方显式给出的会话类型（`model_fields_set`）。"""

    direct: AccessPolicyEntrySchema | None = None
    group: AccessPolicyEntrySchema | None = None


def to_instance_response(
    record: ChannelInstanceRecord,
    *,
    agent_state: str,
    agent_name: str | None,
    connected: bool,
) -> ChannelInstanceResponse:
    return ChannelInstanceResponse(
        id=record.id,
        channel=record.channel,
        display_name=record.display_name,
        owner_ref=record.owner_ref,
        agent_id=record.agent_id,
        agent_name=agent_name,
        agent_state=agent_state,
        status=record.status,
        credential_mask=record.credential_mask,
        config=dict(record.config),
        generation=record.generation,
        connected=connected,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def to_policy_entry(record: ChannelAccessPolicyRecord | None) -> AccessPolicyEntrySchema:
    """缺失的行按默认值返回：**缺失即"放开"**，与判定层的语义一致。"""
    if record is None:
        return AccessPolicyEntrySchema()
    return AccessPolicyEntrySchema(
        mode=record.mode,
        allowlist=list(record.allowlist),
        allow_commands=record.allow_commands,
    )


__all__ = [
    "AccessPolicyEntrySchema",
    "ChannelAccessPolicyResponse",
    "ChannelCapabilitiesResponse",
    "ChannelCatalogItemResponse",
    "ChannelCredentialFieldResponse",
    "ChannelInstanceResponse",
    "ChannelTestRequest",
    "ChannelTestResponse",
    "CreateChannelInstanceRequest",
    "ProvisionBeginRequest",
    "ProvisionResponse",
    "UpdateChannelAccessPolicyRequest",
    "UpdateChannelInstanceRequest",
    "to_instance_response",
    "to_policy_entry",
]
