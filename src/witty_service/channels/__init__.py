"""IM Channel 渠道层。

本模块是 witty-service 面向 IM 平台的外部对话入口：渠道适配器把平台事件
归一化为领域语义，`SessionRouter` 负责路由、队列与出站，`AgentTurnGateway`
是调用既有 `AgentManager` 的窄接口（见 ADR-0001）。

设计依据：`docs/im-channel-framework-design.md`、`docs/im-channel-feature-design.md`。
"""

from __future__ import annotations

from witty_service.channels.contracts import (
    ADAPTER_REGISTRY,
    CERTAINTY_DELIVERED,
    CERTAINTY_REJECTED,
    CERTAINTY_UNCERTAIN,
    CONVERSATION_TYPE_DIRECT,
    CONVERSATION_TYPE_GROUP,
    ChannelAdapter,
    ChannelCapabilities,
    DeliveryResult,
    InboundMessage,
    Route,
    TurnEvent,
    register_adapter,
    registered_channels,
)

__all__ = [
    "ADAPTER_REGISTRY",
    "CERTAINTY_DELIVERED",
    "CERTAINTY_REJECTED",
    "CERTAINTY_UNCERTAIN",
    "CONVERSATION_TYPE_DIRECT",
    "CONVERSATION_TYPE_GROUP",
    "ChannelAdapter",
    "ChannelCapabilities",
    "DeliveryResult",
    "InboundMessage",
    "Route",
    "TurnEvent",
    "register_adapter",
    "registered_channels",
]
