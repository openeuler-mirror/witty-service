"""接入驱动协议与注册表（框架设计 §3.4）。

`ProvisioningFlow` 负责编排与落库，**每个渠道只需实现一个 `ProvisioningDriver`**：
与平台的二维码/凭据交互是唯一的渠道差异。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Protocol, runtime_checkable

#: 接入尝试的进行中状态（驱动返回的 `status` 取值域，与库中 status 一致）
STATUS_WAITING = "waiting"
#: 轮询间隔兜底值（毫秒）：驱动未给出平台建议值时使用
DEFAULT_POLL_INTERVAL_MS = 2000
STATUS_SUCCEEDED = "succeeded"
STATUS_EXPIRED = "expired"
STATUS_FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProvisioningSession:
    """一次接入尝试的开始结果。`state` 是平台侧临时凭据，**只存服务端**。"""

    qr_content: str
    expires_at: datetime
    poll_interval_ms: int
    state: bytes


@dataclass(frozen=True, slots=True)
class ProvisioningOutcome:
    """一次轮询的结果。`credentials` 只在 `succeeded` 时存在。"""

    status: str
    credentials: Mapping[str, str] | None = None
    error_code: str | None = None
    #: 平台可能延长有效期或更换二维码
    qr_content: str | None = None
    expires_at: datetime | None = None


@runtime_checkable
class ProvisioningDriver(Protocol):
    """与平台交互二维码与凭据的驱动。"""

    channel: ClassVar[str]

    async def begin(self) -> ProvisioningSession: ...

    async def poll(self, state: bytes) -> ProvisioningOutcome: ...


#: 渠道标识符 -> 驱动类。与 `ADAPTER_REGISTRY` 同构：驱动模块导入时自注册。
DRIVER_REGISTRY: dict[str, type[ProvisioningDriver]] = {}


def register_driver(driver_cls: type[ProvisioningDriver]) -> type[ProvisioningDriver]:
    channel = getattr(driver_cls, "channel", None)
    if not isinstance(channel, str) or not channel:
        raise ValueError(
            f"Driver {driver_cls!r} must declare a non-empty 'channel' ClassVar."
        )
    existing = DRIVER_REGISTRY.get(channel)
    if existing is not None and existing is not driver_cls:
        raise ValueError(
            f"Provisioning driver for '{channel}' is already registered by "
            f"{existing.__name__}."
        )
    DRIVER_REGISTRY[channel] = driver_cls
    return driver_cls


def get_driver_class(channel: str) -> type[ProvisioningDriver] | None:
    return DRIVER_REGISTRY.get(channel)


def registered_driver_channels() -> tuple[str, ...]:
    return tuple(sorted(DRIVER_REGISTRY))


__all__ = [
    "DEFAULT_POLL_INTERVAL_MS",
    "DRIVER_REGISTRY",
    "STATUS_EXPIRED",
    "STATUS_FAILED",
    "STATUS_SUCCEEDED",
    "STATUS_WAITING",
    "ProvisioningDriver",
    "ProvisioningOutcome",
    "ProvisioningSession",
    "get_driver_class",
    "register_driver",
    "registered_driver_channels",
]
