"""W8 入站去重的测试（特性设计文档 9.1 第 8 条）。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from witty_service.channels.dedup import (
    ChannelMaintenanceTask,
    CleanupOutcome,
    DedupVerdict,
    InboundDedup,
)
from witty_service.persistence.channel_repository import ChannelRepository
from witty_service.persistence.db import create_session_factory, create_sqlite_engine
from witty_service.persistence.orm import Base, ProvisioningStatus


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def repository(tmp_path) -> ChannelRepository:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'dedup.sqlite3'}")
    Base.metadata.create_all(engine)
    yield ChannelRepository(create_session_factory(engine))
    engine.dispose()


def test_first_event_is_accepted_second_is_duplicate(
    repository: ChannelRepository,
) -> None:
    """同一条平台消息投递两次，只处理一次（9.1 第 8 条）。"""
    instance = repository.create_instance(channel="wecom_bot")
    dedup = InboundDedup(repository)

    first = dedup.register(instance_id=instance.id, platform_event_id="evt-1")
    second = dedup.register(instance_id=instance.id, platform_event_id="evt-1")

    assert first is DedupVerdict.accepted
    assert second is DedupVerdict.duplicate


def test_duplicate_within_retention_survives_cleanup(
    repository: ChannelRepository,
) -> None:
    instance = repository.create_instance(channel="wecom_bot")
    dedup = InboundDedup(repository, retention_days=7)
    dedup.register(instance_id=instance.id, platform_event_id="evt-1")

    dedup.cleanup()

    assert (
        dedup.register(instance_id=instance.id, platform_event_id="evt-1")
        is DedupVerdict.duplicate
    )


def test_expired_records_are_cleaned(repository: ChannelRepository) -> None:
    """超期记录被清理，清理后同一标识可再次处理。"""
    instance = repository.create_instance(channel="wecom_bot")
    dedup = InboundDedup(repository, retention_days=7)
    dedup.register(
        instance_id=instance.id,
        platform_event_id="evt-old",
        received_at=_now() - timedelta(days=30),
    )
    dedup.register(instance_id=instance.id, platform_event_id="evt-new")

    outcome = dedup.cleanup()

    assert outcome.inbound_events_removed == 1
    assert repository.count_inbound_events(instance_id=instance.id) == 1
    assert (
        dedup.register(instance_id=instance.id, platform_event_id="evt-old")
        is DedupVerdict.accepted
    )


def test_cleanup_also_reclaims_finished_provisionings(
    repository: ChannelRepository,
) -> None:
    """过期与已取消的接入尝试与去重记录合并清理（框架设计 §3.9）。"""
    repository.create_provisioning(
        channel="wecom_bot",
        owner_ref="expired",
        poll_interval_ms=1000,
        expires_at=_now() - timedelta(minutes=1),
    )
    cancelled = repository.create_provisioning(
        channel="wecom_bot",
        owner_ref="cancelled",
        poll_interval_ms=1000,
        expires_at=_now() + timedelta(minutes=5),
    )
    repository.update_provisioning(
        cancelled.id, status=ProvisioningStatus.cancelled.value
    )

    outcome = InboundDedup(repository).cleanup()

    assert outcome.provisionings_removed == 2
    assert repository.get_provisioning(cancelled.id) is None


@pytest.mark.asyncio
async def test_maintenance_task_is_idempotent(
    repository: ChannelRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    dedup = InboundDedup(repository)
    task = ChannelMaintenanceTask(dedup, interval_seconds=0.01)
    calls: list[int] = []

    def _fake_cleanup(*, now: datetime | None = None) -> CleanupOutcome:
        calls.append(1)
        return CleanupOutcome(inbound_events_removed=0, provisionings_removed=0)

    monkeypatch.setattr(dedup, "cleanup", _fake_cleanup)

    task.start()
    task.start()
    await asyncio.sleep(0.08)
    await task.stop()

    assert task.running is False
    assert calls  # 周期任务确实跑过（重复 start 不会产生第二个任务）


@pytest.mark.asyncio
async def test_maintenance_task_stop_without_start_is_safe(
    repository: ChannelRepository,
) -> None:
    task = ChannelMaintenanceTask(InboundDedup(repository))

    await task.stop()

    assert task.running is False


@pytest.mark.asyncio
async def test_maintenance_task_survives_cleanup_errors(
    repository: ChannelRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清理失败绝不影响渠道网关：只记日志，下个周期重试。"""
    dedup = InboundDedup(repository)
    task = ChannelMaintenanceTask(dedup, interval_seconds=0.01)
    calls: list[int] = []

    def _boom(*, now: datetime | None = None):  # type: ignore[no-untyped-def]
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr(dedup, "cleanup", _boom)

    task.start()
    await asyncio.sleep(0.08)
    await task.stop()

    assert len(calls) > 1
