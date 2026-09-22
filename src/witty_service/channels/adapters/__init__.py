"""渠道适配器子包。

适配器模块在**导入时自注册**进 `contracts.ADAPTER_REGISTRY`；本模块负责触发这些
导入，因此 `ADAPTER_REGISTRY` 的枚举来源与"哪些适配器随包发布"始终一致——契约
测试遍历注册表，新增渠道会自动纳入契约测试。

当前已接入：企业微信机器人（标杆渠道）、QQ 机器人。其余两个渠道（钉钉 / 飞书）
尚未接入，接入时只需在本模块补一行导入。
"""

from __future__ import annotations

from witty_service.channels.adapters.base import BaseChannelAdapter
from witty_service.channels.adapters.qq_bot import QqBotAdapter
from witty_service.channels.adapters.wecom_bot import WecomBotAdapter

__all__ = ["BaseChannelAdapter", "QqBotAdapter", "WecomBotAdapter"]
