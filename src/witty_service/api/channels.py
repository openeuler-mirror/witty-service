"""IM Channel REST 接口（框架设计 §9）。

十三条端点：渠道目录、扫码接入三个、实例六个、连通性测试一个、准入策略两个。

三条纪律：

- 所有端点挂 `/channels` 前缀并复用既有的 Bearer 认证；
- 响应里**只有掩码**，永不回传凭据；平台侧临时凭据（scode / device_code）只在服务端；
- 准入策略写入后**立即生效**：判定层每次从库读取，本模块不做任何进程内快照。
"""

from __future__ import annotations

import logging
from typing import cast

from fastapi import APIRouter, Depends, Query, Request, Response, status

from witty_service.api.auth import require_bearer_auth
from witty_service.api.channel_schemas import (
    AccessPolicyEntrySchema,
    ChannelAccessPolicyResponse,
    ChannelCapabilitiesResponse,
    ChannelCatalogItemResponse,
    ChannelCredentialFieldResponse,
    ChannelInstanceResponse,
    ChannelTestRequest,
    ChannelTestResponse,
    CreateChannelInstanceRequest,
    ProvisionBeginRequest,
    ProvisionResponse,
    UpdateChannelAccessPolicyRequest,
    UpdateChannelInstanceRequest,
    to_instance_response,
    to_policy_entry,
)
from witty_service.api.services import ServiceContainer
from witty_service.channels import commands as cmd
from witty_service.channels import errors as err
from witty_service.channels.access_policy import MODE_OPEN, MODES
from witty_service.channels.adapters.base import resolve_adapter_class
from witty_service.channels.contracts import (
    CERTAINTY_REJECTED,
    CONVERSATION_TYPE_DIRECT,
    CONVERSATION_TYPE_GROUP,
    CONVERSATION_TYPES,
    registered_channels,
)
from witty_service.channels.gateway import ChannelGateway
from witty_service.persistence.channel_repository import (
    UNSET,
    ChannelInstanceRecord,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/channels",
    tags=["channels"],
    dependencies=[Depends(require_bearer_auth)],
)


def get_services(request: Request) -> ServiceContainer:
    return cast(ServiceContainer, request.app.state.services)


def _gateway(services: ServiceContainer) -> ChannelGateway:
    return services.channel_gateway


def _instance_or_404(
    services: ServiceContainer, instance_id: str
) -> ChannelInstanceRecord:
    record = services.channel_repository.get_instance(instance_id)
    if record is None:
        raise err.channel_instance_not_found(instance_id)
    return record


def _to_response(
    services: ServiceContainer, record: ChannelInstanceRecord
) -> ChannelInstanceResponse:
    gateway = _gateway(services)
    turn_gateway = services.channel_turn_gateway
    return to_instance_response(
        record,
        agent_state=turn_gateway.agent_state(record.agent_id),
        agent_name=turn_gateway.agent_name(record.agent_id),
        connected=gateway.is_connected(record.id),
    )


# ==============================================================================
# 渠道目录
# ==============================================================================


@router.get("/catalog", response_model=list[ChannelCatalogItemResponse])
def list_catalog() -> list[ChannelCatalogItemResponse]:
    """已注册渠道的目录：渠道标识符取值域、展示名、手填表单字段与保守能力。

    这一条不在框架文档 §9 的清单里，但前端无法在不硬编码渠道标识符的前提下渲染
    渠道选择与手填表单，因此按"取值域只有一处出处"的原则由服务端给出。
    """
    from witty_service.channels.provisioning.drivers import DRIVER_REGISTRY

    items: list[ChannelCatalogItemResponse] = []
    for channel in registered_channels():
        adapter_cls = resolve_adapter_class(channel)
        if adapter_cls is None:
            continue
        capabilities = adapter_cls.conservative_capabilities()
        items.append(
            ChannelCatalogItemResponse(
                channel=adapter_cls.channel,
                display_name=adapter_cls.display_name or adapter_cls.channel,
                adapter_version=adapter_cls.adapter_version,
                supports_provisioning=adapter_cls.channel in DRIVER_REGISTRY,
                credential_fields=[
                    ChannelCredentialFieldResponse(
                        name=field.name,
                        label=field.label,
                        secret=field.secret,
                        required=field.required,
                    )
                    for field in adapter_cls.credential_fields
                ],
                config_fields=list(adapter_cls.config_fields),
                capabilities=ChannelCapabilitiesResponse(
                    can_edit_message=capabilities.can_edit_message,
                    max_text_length=capabilities.max_text_length,
                    max_reply_segments=capabilities.max_reply_segments,
                ),
            )
        )
    return items


# ==============================================================================
# 扫码接入
# ==============================================================================


@router.post("/provision/begin", response_model=ProvisionResponse)
async def begin_provisioning(
    payload: ProvisionBeginRequest,
    services: ServiceContainer = Depends(get_services),
) -> ProvisionResponse:
    result = await services.get_channel_provisioning().begin(
        channel=payload.channel,
        owner_ref=payload.owner_ref,
        agent_id=payload.agent_id,
    )
    return ProvisionResponse(
        attempt_id=result.attempt_id,
        channel=result.channel,
        status=result.status,
        qr_content=result.qr_content,
        expires_at=result.expires_at,
        poll_interval_ms=result.poll_interval_ms,
        error_code=result.error_code,
    )


@router.get("/provision/{attempt_id}", response_model=ProvisionResponse)
async def get_provisioning(
    attempt_id: str,
    services: ServiceContainer = Depends(get_services),
) -> ProvisionResponse:
    result = await services.get_channel_provisioning().poll(attempt_id)
    instance = (
        None
        if result.instance is None
        else _to_response(services, result.instance)
    )
    attempt = result.attempt
    return ProvisionResponse(
        attempt_id=attempt.attempt_id,
        channel=attempt.channel,
        status=attempt.status,
        qr_content=attempt.qr_content,
        expires_at=attempt.expires_at,
        poll_interval_ms=attempt.poll_interval_ms,
        error_code=attempt.error_code,
        instance=instance,
    )


@router.post("/provision/{attempt_id}/cancel", response_model=ProvisionResponse)
async def cancel_provisioning(
    attempt_id: str,
    services: ServiceContainer = Depends(get_services),
) -> ProvisionResponse:
    attempt = await services.get_channel_provisioning().cancel(attempt_id)
    return ProvisionResponse(
        attempt_id=attempt.attempt_id,
        channel=attempt.channel,
        status=attempt.status,
        qr_content=attempt.qr_content,
        expires_at=attempt.expires_at,
        poll_interval_ms=attempt.poll_interval_ms,
        error_code=attempt.error_code,
    )


# ==============================================================================
# 实例
# ==============================================================================


@router.post(
    "/instances",
    response_model=ChannelInstanceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_instance(
    payload: CreateChannelInstanceRequest,
    services: ServiceContainer = Depends(get_services),
) -> ChannelInstanceResponse:
    """手填凭据旁路：与扫码走**完全相同**的落库顺序与失败回滚。"""
    record = await services.get_channel_manual_binder().bind(
        channel=payload.channel,
        credentials=payload.credentials,
        owner_ref=payload.owner_ref,
        agent_id=payload.agent_id,
        display_name=payload.display_name,
    )
    return _to_response(services, record)


@router.get("/instances", response_model=list[ChannelInstanceResponse])
def list_instances(
    owner_ref: str | None = Query(default=None, description="精确匹配的归属标签"),
    services: ServiceContainer = Depends(get_services),
) -> list[ChannelInstanceResponse]:
    records = services.channel_repository.list_instances(owner_ref=owner_ref)
    return [_to_response(services, record) for record in records]


@router.get("/instances/{instance_id}", response_model=ChannelInstanceResponse)
def get_instance(
    instance_id: str,
    services: ServiceContainer = Depends(get_services),
) -> ChannelInstanceResponse:
    return _to_response(services, _instance_or_404(services, instance_id))


@router.patch("/instances/{instance_id}", response_model=ChannelInstanceResponse)
def update_instance(
    instance_id: str,
    payload: UpdateChannelInstanceRequest,
    services: ServiceContainer = Depends(get_services),
) -> ChannelInstanceResponse:
    """部分更新：只透传调用方显式给出的字段（`agent_id=null` 表示解绑）。"""
    _instance_or_404(services, instance_id)
    provided = payload.model_fields_set
    updated = services.channel_repository.update_instance(
        instance_id,
        agent_id=payload.agent_id if "agent_id" in provided else UNSET,
        display_name=payload.display_name if "display_name" in provided else UNSET,
    )
    if updated is None:  # pragma: no cover - 刚刚校验过存在性
        raise err.channel_instance_not_found(instance_id)
    if "agent_id" in provided:
        # 绑定以库为准（入站管线每次读库），进程内快照同步一份以省一次查询
        services.channel_router.update_instance_agent(instance_id, updated.agent_id)
    return _to_response(services, updated)


@router.delete("/instances/{instance_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_instance(
    instance_id: str,
    services: ServiceContainer = Depends(get_services),
) -> Response:
    _instance_or_404(services, instance_id)
    # 先断开连接再删行：否则长连接会持有已删除实例的回调
    await _gateway(services).disconnect_instance(instance_id)
    services.channel_repository.delete_instance(instance_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/instances/{instance_id}/reconnect", response_model=ChannelInstanceResponse)
async def reconnect_instance(
    instance_id: str,
    services: ServiceContainer = Depends(get_services),
) -> ChannelInstanceResponse:
    _instance_or_404(services, instance_id)
    gateway = _gateway(services)
    if not gateway.running:
        # 网关没起来时"重连"没有意义：明确报错，而不是让调用方以为连上了
        raise err.channel_gateway_disabled(reason=gateway.guard_reason)
    await gateway.reconnect_instance(instance_id)
    record = _instance_or_404(services, instance_id)
    return _to_response(services, record)


@router.post("/instances/{instance_id}/test", response_model=ChannelTestResponse)
async def test_instance(
    instance_id: str,
    payload: ChannelTestRequest,
    services: ServiceContainer = Depends(get_services),
) -> ChannelTestResponse:
    """连通性测试：只发固定文案，不触发回合、不写会话历史。"""
    if payload.conversation_type not in CONVERSATION_TYPES:
        raise err.channel_credentials_invalid(
            channel="", reason=f"unknown conversation_type {payload.conversation_type}"
        )
    _instance_or_404(services, instance_id)
    gateway = _gateway(services)
    if not gateway.running:
        raise err.channel_gateway_disabled(reason=gateway.guard_reason)
    if not gateway.is_connected(instance_id):
        # 按需重连：连通性测试本来就是用来确认"现在能不能发出去"
        await gateway.connect_instance(instance_id)
    if not gateway.is_connected(instance_id):
        # 连不上就不发：返回 rejected 而不是 500/404——调用方要的是三态，不是异常
        return ChannelTestResponse(
            certainty=CERTAINTY_REJECTED,
            error_code=err.CHANNEL_INSTANCE_OFFLINE,
        )
    result = await gateway.send_test_message(
        instance_id=instance_id,
        platform_user_id=payload.platform_user_id,
        conversation_type=payload.conversation_type,
        text=cmd.CONNECTIVITY_TEST_TEXT,
    )
    return ChannelTestResponse(
        certainty=result.certainty,
        error_code=result.error_code,
        platform_message_ref=result.platform_message_ref,
    )


# ==============================================================================
# 准入策略
# ==============================================================================


def _access_policy_response(
    services: ServiceContainer, instance_id: str
) -> ChannelAccessPolicyResponse:
    repository = services.channel_repository
    return ChannelAccessPolicyResponse(
        direct=to_policy_entry(
            repository.get_access_policy(
                instance_id=instance_id, conversation_type=CONVERSATION_TYPE_DIRECT
            )
        ),
        group=to_policy_entry(
            repository.get_access_policy(
                instance_id=instance_id, conversation_type=CONVERSATION_TYPE_GROUP
            )
        ),
    )


@router.get(
    "/instances/{instance_id}/access-policy",
    response_model=ChannelAccessPolicyResponse,
)
def get_access_policy(
    instance_id: str,
    services: ServiceContainer = Depends(get_services),
) -> ChannelAccessPolicyResponse:
    """缺失的策略行按默认值返回：**缺失即"放开"**，与判定层语义一致。"""
    _instance_or_404(services, instance_id)
    return _access_policy_response(services, instance_id)


@router.put(
    "/instances/{instance_id}/access-policy",
    response_model=ChannelAccessPolicyResponse,
)
def put_access_policy(
    instance_id: str,
    payload: UpdateChannelAccessPolicyRequest,
    services: ServiceContainer = Depends(get_services),
) -> ChannelAccessPolicyResponse:
    """写入准入策略；写入后立即生效（判定层每次从库读取，不做进程内快照）。"""
    _instance_or_404(services, instance_id)
    provided = payload.model_fields_set
    for conversation_type in CONVERSATION_TYPES:
        if conversation_type not in provided:
            continue
        entry = getattr(payload, conversation_type)
        if entry is None:
            continue
        services.channel_repository.upsert_access_policy(
            instance_id=instance_id,
            conversation_type=conversation_type,
            mode=_normalize_mode(entry),
            allowlist=list(entry.allowlist),
            allow_commands=entry.allow_commands,
        )
    return _access_policy_response(services, instance_id)


def _normalize_mode(entry: AccessPolicyEntrySchema) -> str:
    mode = (entry.mode or MODE_OPEN).strip().lower()
    if mode not in MODES:
        raise err.channel_credentials_invalid(
            channel="", reason=f"unknown access policy mode {entry.mode}"
        )
    return mode


__all__ = ["router"]
