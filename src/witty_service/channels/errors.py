"""渠道层错误码与域错误构造器。

仓库的既有风格是"码常量放在抛出方模块顶部"；渠道层的错误码被接入编排、
回合网关、路由等多个模块共用，因此集中在本模块，避免同一语义出现多个字面量。

用户可见文案**不在这里**：这里是内部错误的种类，文案统一在
`witty_service.channels.commands`（框架设计 §8.1 的"对用户的表现"一列）。
"""

from __future__ import annotations

from typing import Any

from witty_service.domain.errors import DomainError

# ==============================================================================
# 错误码（框架设计 §8.1）
# ==============================================================================

CHANNEL_INSTANCE_NOT_FOUND = "CHANNEL_INSTANCE_NOT_FOUND"
CHANNEL_AGENT_NOT_BOUND = "CHANNEL_AGENT_NOT_BOUND"
CHANNEL_AGENT_NOT_RUNNABLE = "CHANNEL_AGENT_NOT_RUNNABLE"
CHANNEL_CREDENTIAL_INVALID = "CHANNEL_CREDENTIAL_INVALID"
CHANNEL_PROVISIONING_EXPIRED = "CHANNEL_PROVISIONING_EXPIRED"
CHANNEL_QUEUE_FULL = "CHANNEL_QUEUE_FULL"
CHANNEL_UNSUPPORTED_CONTENT = "CHANNEL_UNSUPPORTED_CONTENT"
CHANNEL_DELIVERY_UNCERTAIN = "CHANNEL_DELIVERY_UNCERTAIN"

# 实现期补充（同为渠道层内部错误种类，不是新的用户可见文案）
CHANNEL_PROVISIONING_NOT_FOUND = "CHANNEL_PROVISIONING_NOT_FOUND"
CHANNEL_PROVISIONING_FAILED = "CHANNEL_PROVISIONING_FAILED"
CHANNEL_PROVISIONING_ALREADY_FINISHED = "CHANNEL_PROVISIONING_ALREADY_FINISHED"
CHANNEL_CREDENTIALS_INVALID = "CHANNEL_CREDENTIALS_INVALID"
CHANNEL_ACCESS_DENIED = "CHANNEL_ACCESS_DENIED"
CHANNEL_TURN_FAILED = "CHANNEL_TURN_FAILED"
CHANNEL_TURN_ABORTED = "CHANNEL_TURN_ABORTED"
CHANNEL_INSTANCE_GENERATION_MISMATCH = "CHANNEL_INSTANCE_GENERATION_MISMATCH"
CHANNEL_ADAPTER_UNKNOWN = "CHANNEL_ADAPTER_UNKNOWN"
CHANNEL_SECRET_KEY_INVALID = "CHANNEL_SECRET_KEY_INVALID"
CHANNEL_GATEWAY_DISABLED = "CHANNEL_GATEWAY_DISABLED"
CHANNEL_INSTANCE_OFFLINE = "CHANNEL_INSTANCE_OFFLINE"


# ==============================================================================
# 构造器
# ==============================================================================


def channel_error(
    code: str,
    message: str,
    *,
    status_code: int = 400,
    details: dict[str, Any] | None = None,
) -> DomainError:
    return DomainError(
        code=code, message=message, status_code=status_code, details=details
    )


def channel_instance_not_found(instance_id: str) -> DomainError:
    return channel_error(
        CHANNEL_INSTANCE_NOT_FOUND,
        "Channel instance was not found.",
        status_code=404,
        details={"instance_id": instance_id},
    )


def channel_agent_not_bound(*, instance_id: str, agent_id: str | None = None) -> DomainError:
    """实例没有可用 agent：从未绑定，或绑定后被删除（对用户呈现同一文案）。"""
    details: dict[str, Any] = {"instance_id": instance_id}
    if agent_id is not None:
        details["agent_id"] = agent_id
    return channel_error(
        CHANNEL_AGENT_NOT_BOUND,
        "Channel instance has no usable agent bound.",
        status_code=409,
        details=details,
    )


def channel_agent_not_runnable(
    *, agent_id: str, status: str, agent_name: str | None = None
) -> DomainError:
    return channel_error(
        CHANNEL_AGENT_NOT_RUNNABLE,
        "Agent is not runnable for channel messages.",
        status_code=409,
        details={"agent_id": agent_id, "status": status, "agent_name": agent_name},
    )


def channel_queue_full(*, route_key: tuple[str, str, str], depth: int) -> DomainError:
    return channel_error(
        CHANNEL_QUEUE_FULL,
        "Channel route queue is full.",
        status_code=429,
        details={"route": list(route_key), "depth": depth},
    )


def channel_unsupported_content(*, kind: str | None) -> DomainError:
    return channel_error(
        CHANNEL_UNSUPPORTED_CONTENT,
        "Channel received an unsupported content type.",
        status_code=400,
        details={"kind": kind},
    )


def channel_credential_invalid(*, instance_id: str, reason: str | None = None) -> DomainError:
    return channel_error(
        CHANNEL_CREDENTIAL_INVALID,
        "Channel credentials were rejected by the platform.",
        status_code=409,
        details={"instance_id": instance_id, "reason": reason},
    )


def channel_delivery_uncertain(
    *, instance_id: str, error_code: str | None = None
) -> DomainError:
    return channel_error(
        CHANNEL_DELIVERY_UNCERTAIN,
        "Channel delivery outcome is uncertain; not retrying.",
        status_code=502,
        details={"instance_id": instance_id, "error_code": error_code},
    )


def channel_provisioning_not_found(attempt_id: str) -> DomainError:
    return channel_error(
        CHANNEL_PROVISIONING_NOT_FOUND,
        "Provisioning attempt was not found.",
        status_code=404,
        details={"attempt_id": attempt_id},
    )


def channel_provisioning_expired(*, attempt_id: str) -> DomainError:
    return channel_error(
        CHANNEL_PROVISIONING_EXPIRED,
        "Provisioning attempt has expired.",
        status_code=410,
        details={"attempt_id": attempt_id},
    )


def channel_provisioning_unavailable(*, channel: str, reason: str) -> DomainError:
    """接入驱动**无法开始**（网络不可达 / 平台返回错误 / 协议不符）。

    与 `channel_provisioning_failed` 的区别：那一个是"尝试已经开始但失败了"（有
    attempt_id），这一个连尝试都没能建立，因此不会在库里留下任何行。
    """
    return channel_error(
        CHANNEL_PROVISIONING_FAILED,
        "Provisioning driver could not start.",
        status_code=502,
        details={"channel": channel, "reason": reason},
    )


def channel_provisioning_failed(
    *, attempt_id: str, reason: str | None = None
) -> DomainError:
    return channel_error(
        CHANNEL_PROVISIONING_FAILED,
        "Provisioning attempt failed.",
        status_code=502,
        details={"attempt_id": attempt_id, "reason": reason},
    )


def channel_credentials_invalid(*, channel: str, reason: str) -> DomainError:
    return channel_error(
        CHANNEL_CREDENTIALS_INVALID,
        "Manual credentials were rejected.",
        status_code=400,
        details={"channel": channel, "reason": reason},
    )


def channel_adapter_unknown(*, channel: str) -> DomainError:
    return channel_error(
        CHANNEL_ADAPTER_UNKNOWN,
        "Channel identifier is not registered.",
        status_code=400,
        details={"channel": channel},
    )


def channel_secret_key_invalid(*, reason: str) -> DomainError:
    return channel_error(
        CHANNEL_SECRET_KEY_INVALID,
        "WITTY_CHANNEL_SECRET_KEY is missing or invalid.",
        status_code=500,
        details={"reason": reason},
    )


def channel_instance_offline(
    *, instance_id: str, status: str | None = None, reason: str | None = None
) -> DomainError:
    """实例存在但**没有可用连接**：主动出站（连通性测试）无法执行。

    与 `channel_instance_not_found` 的区别很重要：前者是"实例不存在"，这个是
    "实例在，但连不上平台"——调用方的处置完全不同（改 id vs 重新接入/查网络）。
    """
    details: dict[str, Any] = {"instance_id": instance_id}
    if status is not None:
        details["status"] = status
    if reason is not None:
        details["reason"] = reason
    return channel_error(
        CHANNEL_INSTANCE_OFFLINE,
        "Channel instance has no usable connection.",
        status_code=409,
        details=details,
    )


def channel_gateway_disabled(*, reason: str | None = None) -> DomainError:
    """渠道网关没有运行（守卫拒绝或总开关关闭）：需要连接的动作无法执行。"""
    return channel_error(
        CHANNEL_GATEWAY_DISABLED,
        "Channel gateway is not running.",
        status_code=409,
        details={"reason": reason},
    )


def channel_turn_aborted(*, session_id: str) -> DomainError:
    return channel_error(
        CHANNEL_TURN_ABORTED,
        "Channel turn was aborted.",
        status_code=409,
        details={"session_id": session_id},
    )


def channel_turn_failed(
    *, session_id: str, code: str | None = None, message: str | None = None
) -> DomainError:
    return channel_error(
        CHANNEL_TURN_FAILED,
        "Channel turn failed before reaching a terminal event.",
        status_code=502,
        details={"session_id": session_id, "upstream_code": code, "upstream_message": message},
    )


__all__ = [
    "CHANNEL_ACCESS_DENIED",
    "CHANNEL_ADAPTER_UNKNOWN",
    "CHANNEL_AGENT_NOT_BOUND",
    "CHANNEL_AGENT_NOT_RUNNABLE",
    "CHANNEL_CREDENTIALS_INVALID",
    "CHANNEL_CREDENTIAL_INVALID",
    "CHANNEL_DELIVERY_UNCERTAIN",
    "CHANNEL_GATEWAY_DISABLED",
    "CHANNEL_INSTANCE_GENERATION_MISMATCH",
    "CHANNEL_INSTANCE_NOT_FOUND",
    "CHANNEL_PROVISIONING_ALREADY_FINISHED",
    "CHANNEL_PROVISIONING_EXPIRED",
    "CHANNEL_PROVISIONING_FAILED",
    "CHANNEL_PROVISIONING_NOT_FOUND",
    "CHANNEL_QUEUE_FULL",
    "CHANNEL_SECRET_KEY_INVALID",
    "CHANNEL_TURN_ABORTED",
    "CHANNEL_TURN_FAILED",
    "CHANNEL_UNSUPPORTED_CONTENT",
    "channel_adapter_unknown",
    "channel_agent_not_bound",
    "channel_agent_not_runnable",
    "channel_credential_invalid",
    "channel_credentials_invalid",
    "channel_delivery_uncertain",
    "channel_error",
    "channel_instance_not_found",
    "channel_provisioning_expired",
    "channel_provisioning_failed",
    "channel_provisioning_not_found",
    "channel_queue_full",
    "channel_secret_key_invalid",
    "channel_turn_aborted",
    "channel_turn_failed",
    "channel_unsupported_content",
]
