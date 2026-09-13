"""适配器骨架：能力声明 + 生命周期模板方法 + 三态归类 + 凭据掩码。

三条纪律（框架设计 §3.2 / §8.2）：

- **能力是运行期事实**：探测在 `start()` 内完成，探测失败退回保守值而不是抛异常；
- **适配器不抛业务异常**：平台错误一律经 `classify_exception` 归类为三态返回；
- **判定错误的代价不对称**：把 `uncertain` 误判为 `rejected` 会导致重复投递，因此
  无法确定的异常**默认归为 `uncertain``**。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

from witty_service.channels import errors as err
from witty_service.channels.contracts import (
    ADAPTER_REGISTRY,
    ChannelCapabilities,
    DeliveryResult,
    InboundHandler,
    InboundMessage,
    Route,
)


@dataclass(frozen=True, slots=True)
class CredentialField:
    """手填凭据表单的一个字段：**由适配器声明驱动前端表单**，前端不硬编码字段名。"""

    name: str
    label: str
    #: True 表示该字段是密文（落库加密、响应里只出现掩码）
    secret: bool = True
    required: bool = True


@dataclass(frozen=True, slots=True)
class CredentialMaterial:
    """手填/扫码得到的凭据被拆成三份：非密配置、密文字段、可展示掩码。"""

    config: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    mask: str = ""


class BaseChannelAdapter(ABC):
    """所有渠道适配器的公共骨架。"""

    #: 渠道标识符（= `ADAPTER_REGISTRY` 的键）
    channel: ClassVar[str] = ""
    #: 适配器版本（`/version` 展示用；按实施计划 W1，与 `channel` 并列而非放进能力声明）
    adapter_version: ClassVar[str] = "0.0.0"

    #: 渠道展示名（接入页面的渠道选择与实例列表用）；空表示直接用 `channel`
    display_name: ClassVar[str] = ""
    #: 凭据中属于**非密配置**的字段（平台侧标识、网关地址等）
    config_fields: ClassVar[tuple[str, ...]] = ()
    #: 凭据中可生成**掩码**的字段（只用于确认"这是哪一份凭据"，不参与鉴权）
    mask_fields: ClassVar[tuple[str, ...]] = ()
    #: 手填凭据的必填字段（缺失即拒绝）；留空表示不做必填校验
    required_credentials: ClassVar[tuple[str, ...]] = ()
    #: 手填凭据表单的字段声明（顺序即表单顺序）
    credential_fields: ClassVar[tuple[CredentialField, ...]] = ()

    def __init__(
        self,
        *,
        instance_id: str = "",
        config: Mapping[str, Any] | None = None,
        credentials: Mapping[str, str] | None = None,
    ) -> None:
        self._instance_id = instance_id
        self._config: dict[str, Any] = dict(config or {})
        self._credentials: dict[str, str] = dict(credentials or {})
        self._handler: InboundHandler | None = None
        self._capabilities: ChannelCapabilities | None = None
        self._started = False

    # ==========================================================================
    # 能力声明
    # ==========================================================================

    @classmethod
    def conservative_capabilities(cls) -> ChannelCapabilities:
        """保守默认值：更短的单条上限、不能原地编辑（`start()` 之前返回它）。"""
        return ChannelCapabilities.conservative()

    def capabilities(self) -> ChannelCapabilities:
        return self._capabilities or self.conservative_capabilities()

    # ==========================================================================
    # 入站回调
    # ==========================================================================

    def on_inbound(self, handler: InboundHandler) -> None:
        self._handler = handler

    @property
    def has_inbound_handler(self) -> bool:
        return self._handler is not None

    async def emit_inbound(self, message: InboundMessage) -> None:
        """把归一化后的消息上报给统一管线（未注册回调时丢弃并记日志）。"""
        if self._handler is None:
            return
        await self._handler(message)

    # ==========================================================================
    # 生命周期模板方法（幂等）
    # ==========================================================================

    @property
    def started(self) -> bool:
        return self._started

    def is_alive(self) -> bool:
        """连接是否仍然可用（健康复查用）。

        默认与 `started` 等价；**长连接类渠道必须覆写**——它们的接收循环可能在
        `start()` 之后因对端断开而退出，此时 `started` 仍为 True，但连接已经死了。
        """
        return self._started

    async def start(self) -> None:
        """确保连接可用（**幂等**）：已连接时是空操作，连接已死时**重连**。

        语义是"确保已连接"而不是"只在第一次启动"：网关的健康复查发现
        `is_alive()` 为假时会再次调用 `start()`。早期版本在 `_started` 为真时直接
        返回，导致"连接已死"的适配器永远无法被重建——网关每 15s 记一条"已连接"
        日志，实际一次都没重连，实例在 UI 上永远显示未连接。
        """
        if self._started and self.is_alive():
            return
        if self._started:
            # 连接已死：先收干净旧连接（旧接收循环 + 旧 socket），再重建
            await self._disconnect()
            self._started = False
        await self._connect()
        self._started = True
        try:
            self._capabilities = await self._probe_capabilities()
        except Exception:
            # 探测失败必须退回保守值，而不是让渠道起不来
            self._capabilities = self.conservative_capabilities()

    async def stop(self) -> None:
        """断开连接；未启动或重复调用都是空操作。"""
        if not self._started:
            return
        self._started = False
        self._capabilities = None
        await self._disconnect()

    @abstractmethod
    async def _connect(self) -> None: ...

    @abstractmethod
    async def _disconnect(self) -> None: ...

    async def _probe_capabilities(self) -> ChannelCapabilities:
        """默认返回保守值；各渠道可覆写（平台探测 / SDK 常量 / 配置）。"""
        return self.conservative_capabilities()

    # ==========================================================================
    # 出站（子类实现）
    # ==========================================================================

    @abstractmethod
    async def send_text(self, route: Route, text: str) -> DeliveryResult: ...

    async def edit_text(
        self, route: Route, message_ref: str, text: str
    ) -> DeliveryResult:
        """默认实现：不声明原地编辑能力的渠道不该被调用到。

        返回 `uncertain` 而不是抛异常——契约测试与上层都按"三态"处理。
        """
        del route, message_ref, text
        return DeliveryResult.uncertain_with(err.CHANNEL_DELIVERY_UNCERTAIN)

    # ==========================================================================
    # 三态归类
    # ==========================================================================

    def classify_exception(self, exc: BaseException) -> DeliveryResult:
        """把平台/SDK/协议异常归类为三态。

        默认实现一律返回 `uncertain`：无法确定时**不重发**，代价远小于重复投递。
        各渠道覆写本方法给出自己的"异常 → 三态"映射。
        """
        del exc
        return DeliveryResult.uncertain_with(err.CHANNEL_DELIVERY_UNCERTAIN)

    # ==========================================================================
    # 凭据
    # ==========================================================================

    @classmethod
    def split_credentials(
        cls, credentials: Mapping[str, str]
    ) -> CredentialMaterial:
        """把平台凭据拆成"非密配置 / 密文字段 / 掩码"。

        `config_fields` 之外的所有字段一律当密文处理——**默认加密**，只有显式声明的
        非密字段才会以明文落在 `channel_instances.config`。
        """
        config: dict[str, Any] = {}
        secrets: dict[str, str] = {}
        for key, value in credentials.items():
            if key in cls.config_fields:
                config[key] = value
            else:
                secrets[key] = value
        mask = ",".join(
            cls.mask_credential(credentials[field])
            for field in cls.mask_fields
            if credentials.get(field)
        )
        return CredentialMaterial(config=config, secrets=secrets, mask=mask)

    @staticmethod
    def mask_credential(value: str) -> str:
        """掩码规则：保留首 4 位与末 4 位，中间 4 个星号；不足 8 位全部打码。"""
        if len(value) < 8:
            return "*" * len(value)
        return f"{value[:4]}****{value[-4:]}"

    @classmethod
    def build_credential_mask(cls, credentials: Mapping[str, str]) -> str:
        return cls.split_credentials(credentials).mask

    # ==========================================================================
    # 工具
    # ==========================================================================

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def config(self) -> dict[str, Any]:
        return dict(self._config)

    @property
    def credentials(self) -> dict[str, str]:
        return dict(self._credentials)


def resolve_adapter_class(channel: str) -> type[BaseChannelAdapter] | None:
    """按渠道标识符取适配器类，并确认它实现了本骨架的契约。

    返回 None 表示"未知渠道"或"没有按骨架实现"，两种都由调用方按未知渠道处理
    （`CHANNEL_ADAPTER_UNKNOWN`）；数据库中的 `channel` 取值域就是
    `ADAPTER_REGISTRY` 的键。
    """
    adapter_cls = ADAPTER_REGISTRY.get(channel)
    if adapter_cls is None or not issubclass(adapter_cls, BaseChannelAdapter):
        return None
    return adapter_cls


__all__ = [
    "BaseChannelAdapter",
    "CredentialField",
    "CredentialMaterial",
    "resolve_adapter_class",
]
