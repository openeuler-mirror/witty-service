"""入站事件去重与周期清理（框架设计 §3.9）。

**为什么用数据库而不是进程内环形缓冲**：重复投递最常发生在重连时，而重连最常
发生在服务重启前后——进程内缓冲恰好在这一刻失效。

同一个周期任务（默认每 6 小时）还负责把**已过期 / 已取消的接入尝试**清理掉，
因此 `channel_provisionings` 的增长也是有界的。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from witty_service.persistence.channel_repository import (
    ChannelRepository,
    retention_cutoff,
)

logger = logging.getLogger(__name__)

#: 清理周期（框架设计 §3.9：每 6 小时一次）
DEFAULT_CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60
#: 入站去重记录保留期默认值（环境变量 `WITTY_CHANNEL_INBOUND_RETENTION_DAYS`）
DEFAULT_RETENTION_DAYS = 7


class DedupVerdict(StrEnum):
    accepted = "accepted"
    duplicate = "duplicate"


@dataclass(frozen=True, slots=True)
class CleanupOutcome:
    inbound_events_removed: int
    provisionings_removed: int


class InboundDedup:
    """`(渠道实例, 平台事件标识)` 唯一约束落库，冲突即视为重复投递并丢弃。"""

    def __init__(
        self,
        repository: ChannelRepository,
        *,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self._repository = repository
        self._retention_days = max(1, retention_days)

    @property
    def retention_days(self) -> int:
        return self._retention_days

    def register(
        self,
        *,
        instance_id: str,
        platform_event_id: str,
        received_at: datetime | None = None,
    ) -> DedupVerdict:
        """登记一次入站事件；重复投递返回 `duplicate`（调用方丢弃，不回复）。"""
        accepted = self._repository.register_inbound_event(
            instance_id=instance_id,
            platform_event_id=platform_event_id,
            received_at=received_at,
        )
        return DedupVerdict.accepted if accepted else DedupVerdict.duplicate

    def cleanup(self, *, now: datetime | None = None) -> CleanupOutcome:
        """回收超期去重记录与已结束的接入尝试（两者合并为同一个周期任务）。"""
        moment = now or datetime.now(UTC)
        removed_events = self._repository.prune_inbound_events(
            before=retention_cutoff(days=self._retention_days, now=moment)
        )
        removed_provisionings = self._repository.prune_provisionings(now=moment)
        return CleanupOutcome(
            inbound_events_removed=removed_events,
            provisionings_removed=removed_provisionings,
        )


class ChannelMaintenanceTask:
    """周期清理任务：由 `ChannelGateway` 在启动时注册、关闭时取消（框架设计 §7.2）。"""

    def __init__(
        self,
        dedup: InboundDedup,
        *,
        interval_seconds: float = DEFAULT_CLEANUP_INTERVAL_SECONDS,
    ) -> None:
        self._dedup = dedup
        # 下限只用于避免忙循环；测试可注入更小的间隔
        self._interval_seconds = max(0.01, float(interval_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """启动周期清理（幂等：重复调用不会产生第二个任务）。"""
        if self.running:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        self._stopping = True
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def run_once(self) -> CleanupOutcome:
        outcome = self._dedup.cleanup()
        if outcome.inbound_events_removed or outcome.provisionings_removed:
            logger.info(
                "Channel cleanup removed inbound_events=%d provisionings=%d",
                outcome.inbound_events_removed,
                outcome.provisionings_removed,
            )
        return outcome

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(self._interval_seconds)
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 清理失败绝不影响渠道网关：只记日志，下个周期重试
                logger.warning("Channel periodic cleanup failed", exc_info=True)


__all__ = [
    "DEFAULT_CLEANUP_INTERVAL_SECONDS",
    "DEFAULT_RETENTION_DAYS",
    "ChannelMaintenanceTask",
    "CleanupOutcome",
    "DedupVerdict",
    "InboundDedup",
]
