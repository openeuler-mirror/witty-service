"""渠道层契约：类型定义、能力声明、适配器协议与适配器注册表。

本模块是渠道层唯一的公共词汇表（框架设计 §3.2）。所有跨模块传递的值都取自
这里的定义，不在别处新造类型。

设计约束（同样来自 §3.2）：

- **能力是运行期事实**：同一个渠道在不同实例、不同权限下能力可能不同，因此
  `capabilities()` 是实例方法；探测在 `start()` 内完成，`start()` 之前返回保守默认值；
- **适配器不抛业务异常**：平台错误一律归类为 `DeliveryResult` 三态；
- **适配器不做分段**：分段由 `delivery.DeliveryPlanner` 统一负责；
- **不支持的内容也上报**：`text=None` + `unsupported_kind`，由上层统一回复降级文案。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Protocol, runtime_checkable

# ==============================================================================
# 取值域常量
# ==============================================================================

#: 聊天类型（框架设计 §5.1 的 conversation_type；MVP 运行期只产生 direct）
CONVERSATION_TYPE_DIRECT = "direct"
CONVERSATION_TYPE_GROUP = "group"
CONVERSATION_TYPES = (CONVERSATION_TYPE_DIRECT, CONVERSATION_TYPE_GROUP)

#: 投递确定性三态（框架设计 §8.2）
CERTAINTY_DELIVERED = "delivered"
CERTAINTY_REJECTED = "rejected"
CERTAINTY_UNCERTAIN = "uncertain"
DELIVERY_CERTAINTIES = (CERTAINTY_DELIVERED, CERTAINTY_REJECTED, CERTAINTY_UNCERTAIN)

#: 不支持的内容类型取值域（与 `text=None` 配合使用）
UNSUPPORTED_IMAGE = "image"
UNSUPPORTED_FILE = "file"
UNSUPPORTED_VOICE = "voice"
UNSUPPORTED_UNKNOWN = "unknown"
UNSUPPORTED_KINDS = (
    UNSUPPORTED_IMAGE,
    UNSUPPORTED_FILE,
    UNSUPPORTED_VOICE,
    UNSUPPORTED_UNKNOWN,
)

#: 保守的单条文本长度上限：能力探测失败或尚未探测时使用（框架设计 §3.2）
CONSERVATIVE_MAX_TEXT_LENGTH = 1000


# ==============================================================================
# 值对象
# ==============================================================================


@dataclass(frozen=True, slots=True)
class Route:
    """会话路由：唯一确定一条入站消息属于哪个机器人、哪个聊天、哪个用户。"""

    instance_id: str
    conversation_type: str
    platform_user_id: str

    @property
    def key(self) -> tuple[str, str, str]:
        """路由键归一化：三元组的元组形式，用作进程内字典的键。"""
        return (self.instance_id, self.conversation_type, self.platform_user_id)


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """归一化后的入站消息。"""

    platform_event_id: str
    route: Route
    #: None 表示该内容类型当前不支持（图片/文件/语音等）
    text: str | None
    #: 配合 text=None 使用，取值域见 UNSUPPORTED_KINDS
    unsupported_kind: str | None = None
    received_at: datetime | None = None

    @property
    def is_text(self) -> bool:
        return self.text is not None


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """一次出站发送的三态结果。`delivered` 时必须带平台消息引用。"""

    certainty: str
    platform_message_ref: str | None = None
    error_code: str | None = None

    @property
    def delivered(self) -> bool:
        return self.certainty == CERTAINTY_DELIVERED

    @property
    def rejected(self) -> bool:
        """平台**确定性**拒绝：已确认没有送达，允许降级重投。"""
        return self.certainty == CERTAINTY_REJECTED

    @property
    def uncertain(self) -> bool:
        """结果不确定：可能已经送达，**不得**重发。"""
        return self.certainty == CERTAINTY_UNCERTAIN

    @classmethod
    def delivered_with(cls, message_ref: str) -> DeliveryResult:
        return cls(certainty=CERTAINTY_DELIVERED, platform_message_ref=message_ref)

    @classmethod
    def rejected_with(cls, error_code: str) -> DeliveryResult:
        return cls(certainty=CERTAINTY_REJECTED, error_code=error_code)

    @classmethod
    def uncertain_with(cls, error_code: str) -> DeliveryResult:
        return cls(certainty=CERTAINTY_UNCERTAIN, error_code=error_code)


@dataclass(frozen=True, slots=True)
class ChannelCapabilities:
    """渠道实例在运行期声明的能力。MVP 只声明会被分支判定的能力。"""

    #: False 时 edit_text 不会被调用（契约测试会断言这一点）
    can_edit_message: bool
    #: 单条消息字符上限，必须为正整数
    max_text_length: int
    #: 一次回复允许的最大条数，None 表示不设限
    max_reply_segments: int | None = None

    @classmethod
    def conservative(cls) -> ChannelCapabilities:
        """探测失败或尚未探测时的保守值（更短的单条上限、不可原地编辑）。"""
        return cls(
            can_edit_message=False,
            max_text_length=CONSERVATIVE_MAX_TEXT_LENGTH,
            max_reply_segments=None,
        )


#: 回合事件直接复用既有 envelope，不新造类型（框架设计 §3.2）：
#:   {"type": str, "session_id": str, "runtime_type": str, "event_id": str,
#:    "ts_ms": int, "payload": dict}
#: 渠道层只消费四个 type：message.delta / message.completed / question.asked /
#: stream.error / client.error；其余 type 一律忽略，不作分支。
TurnEvent = dict[str, Any]

#: 回合终态事件类型（先到者即终态）
TERMINAL_EVENT_TYPES = ("message.completed", "turn.completed")
#: 回合失败事件类型
ERROR_EVENT_TYPES = ("stream.error", "client.error")

InboundHandler = Callable[[InboundMessage], Awaitable[None]]


# ==============================================================================
# 适配器协议与注册表
# ==============================================================================


@runtime_checkable
class ChannelAdapter(Protocol):
    """渠道适配器协议：只负责"渠道语言 ↔ 领域语义"的翻译。

    `channel` / `adapter_version` 是类属性：前者是 `ADAPTER_REGISTRY` 的键，
    后者供 `/version` 展示（因此**不**放进 `capabilities()`——`capabilities()`
    只声明会被分支判定的能力）。
    """

    channel: ClassVar[str]
    adapter_version: ClassVar[str]

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def is_alive(self) -> bool: ...

    def capabilities(self) -> ChannelCapabilities: ...

    async def send_text(self, route: Route, text: str) -> DeliveryResult: ...

    async def edit_text(
        self, route: Route, message_ref: str, text: str
    ) -> DeliveryResult: ...

    def on_inbound(self, handler: InboundHandler) -> None: ...


#: 渠道标识符取值的唯一出处：数据库 channel_instances.channel、接入接口的
#: channel 参数、契约测试的枚举来源，全部取自这里的键。
ADAPTER_REGISTRY: dict[str, type[ChannelAdapter]] = {}


def register_adapter(adapter_cls: type[ChannelAdapter]) -> type[ChannelAdapter]:
    """把适配器类注册进 `ADAPTER_REGISTRY`（适配器模块导入时自注册）。

    重复注册不同的类会直接抛错——渠道标识符是数据库取值域，静默覆盖会让
    已落库的实例在升级后接到另一个渠道的连接。
    """
    channel = getattr(adapter_cls, "channel", None)
    if not isinstance(channel, str) or not channel:
        raise ValueError(
            f"Adapter {adapter_cls!r} must declare a non-empty 'channel' ClassVar."
        )
    existing = ADAPTER_REGISTRY.get(channel)
    if existing is not None and existing is not adapter_cls:
        raise ValueError(
            f"Channel '{channel}' is already registered by {existing.__name__}."
        )
    ADAPTER_REGISTRY[channel] = adapter_cls
    return adapter_cls


def get_adapter_class(channel: str) -> type[ChannelAdapter] | None:
    """按渠道标识符取适配器类；未注册返回 None（由装配方记错误日志并跳过）。"""
    return ADAPTER_REGISTRY.get(channel)


def registered_channels() -> tuple[str, ...]:
    """已注册渠道标识符（排序后的快照，供契约测试与接口校验使用）。"""
    return tuple(sorted(ADAPTER_REGISTRY))


__all__ = [
    "ADAPTER_REGISTRY",
    "CERTAINTY_DELIVERED",
    "CERTAINTY_REJECTED",
    "CERTAINTY_UNCERTAIN",
    "CONSERVATIVE_MAX_TEXT_LENGTH",
    "CONVERSATION_TYPES",
    "CONVERSATION_TYPE_DIRECT",
    "CONVERSATION_TYPE_GROUP",
    "DELIVERY_CERTAINTIES",
    "ERROR_EVENT_TYPES",
    "TERMINAL_EVENT_TYPES",
    "UNSUPPORTED_FILE",
    "UNSUPPORTED_IMAGE",
    "UNSUPPORTED_KINDS",
    "UNSUPPORTED_UNKNOWN",
    "UNSUPPORTED_VOICE",
    "ChannelAdapter",
    "ChannelCapabilities",
    "DeliveryResult",
    "InboundHandler",
    "InboundMessage",
    "Route",
    "TurnEvent",
    "get_adapter_class",
    "register_adapter",
    "registered_channels",
]
