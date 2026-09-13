"""手填凭据旁路（框架设计 §8.1 的"救援通道"）。

两个用途：扫码不可用时的手工接入，以及**凭据遗失后重新接入的唯一方式**。

硬性要求：手填路径与扫码路径走**完全相同**的落库顺序与失败回滚
（`provisioning.flow.persist_instance_from_credentials`），不允许出现第二套写入逻辑。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from witty_service.channels import errors as err
from witty_service.channels.adapters.base import resolve_adapter_class
from witty_service.channels.provisioning.flow import (
    InstanceReadyHook,
    persist_instance_from_credentials,
)
from witty_service.persistence.channel_repository import (
    ChannelInstanceRecord,
    ChannelRepository,
)

logger = logging.getLogger(__name__)


class ManualCredentialBinder:
    """把调用方直接提交的平台凭据落成渠道实例。"""

    def __init__(
        self,
        *,
        repository: ChannelRepository,
        cipher: Any,
        on_instance_ready: InstanceReadyHook | None = None,
    ) -> None:
        self._repository = repository
        self._cipher = cipher
        self._on_instance_ready = on_instance_ready

    async def bind(
        self,
        *,
        channel: str,
        credentials: Mapping[str, str],
        owner_ref: str | None = None,
        agent_id: str | None = None,
        display_name: str | None = None,
    ) -> ChannelInstanceRecord:
        if resolve_adapter_class(channel) is None:
            raise err.channel_adapter_unknown(channel=channel)
        material_input = {str(k): str(v) for k, v in credentials.items() if v}
        if not material_input:
            raise err.channel_credentials_invalid(
                channel=channel, reason="credentials must not be empty"
            )

        instance = persist_instance_from_credentials(
            repository=self._repository,
            cipher=self._cipher,
            channel=channel,
            credentials=material_input,
            owner_ref=owner_ref,
            agent_id=agent_id,
            display_name=display_name,
        )
        if self._on_instance_ready is not None:
            try:
                await self._on_instance_ready(instance)
            except Exception:
                # 装配失败不影响"接入已成功"：实例已落库，重连接口可再触发
                logger.warning(
                    "Failed to notify channel gateway about manually bound instance: %s",
                    instance.id,
                    exc_info=True,
                )
        return instance


__all__ = ["ManualCredentialBinder"]
