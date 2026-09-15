"""W14 渠道网关生命周期的测试：三个守卫分支 + 装配、监督、关闭（框架设计 §3.3、§7）。

全部用 `fakes.FakeAdapter` 驱动，不建立任何真实连接；`sleep` 与 `clock` 都是注入的，
因此测试是确定性的（不依赖真实时间等待）。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from tests.unit.channels.fakes import FakeAdapter, FakeTurnGateway, FakeTurnScript
from witty_service.channels import errors as err
from witty_service.channels.contracts import (
    CONVERSATION_TYPE_DIRECT,
    ChannelAdapter,
    InboundMessage,
    Route,
)
from witty_service.channels.dedup import InboundDedup
from witty_service.channels.gateway import BACKOFF_SEQUENCE_SECONDS, ChannelGateway
from witty_service.channels.router import SessionRouter
from witty_service.config import ChannelSettings
from witty_service.domain.errors import DomainError
from witty_service.persistence.channel_repository import ChannelRepository
from witty_service.persistence.db import create_session_factory, create_sqlite_engine
from witty_service.persistence.orm import Base, ChannelInstanceStatus

HEALTH_INTERVAL = 60.0


@dataclass
class Harness:
    gateway: ChannelGateway
    repository: ChannelRepository
    router: SessionRouter
    turn_gateway: FakeTurnGateway
    dedup: InboundDedup
    instance_id: str = ""
    #: instance_id -> 该实例装配出的假适配器
    adapters: dict[str, FakeAdapter] = field(default_factory=dict)
    sleeps: list[float] = field(default_factory=list)
    now: float = 1000.0

    @property
    def adapter(self) -> FakeAdapter:
        assert self.adapters, "no adapter was built"
        return next(iter(self.adapters.values()))

    def route(self, user: str = "u1") -> Route:
        return Route(
            instance_id=self.instance_id,
            conversation_type=CONVERSATION_TYPE_DIRECT,
            platform_user_id=user,
        )

    async def settle(self) -> None:
        """让监督循环完成首次监督（`start()` 之后它只跑一次就会进入注入的睡眠）。"""
        for _ in range(5):
            await asyncio.sleep(0)

    async def send(self, text: str, *, event_id: str) -> None:
        await self.router.handle_inbound(
            InboundMessage(
                platform_event_id=event_id,
                route=self.route(),
                text=text,
                received_at=datetime.now(UTC),
            )
        )

    async def idle(self, *, timeout: float = 2.0) -> bool:
        return await self.router.wait_until_idle(self.route(), timeout=timeout)


def _build(
    tmp_path,
    *,
    workers: int | None = 1,
    credentials_dir: str | None = None,
    enabled: bool = True,
    instance_channel: str = "fake_bot",
    with_instance: bool = True,
    adapter_start_error: BaseException | None = None,
) -> Harness:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'gateway.sqlite3'}")
    Base.metadata.create_all(engine)
    repository = ChannelRepository(create_session_factory(engine))
    instance_id = ""
    if with_instance:
        instance_id = repository.create_instance(
            channel=instance_channel, agent_id="agent-1"
        ).id

    turn_gateway = FakeTurnGateway(
        agent_states={"agent-1": "running"}, agent_names={"agent-1": "demo-agent"}
    )
    dedup = InboundDedup(repository)
    router = SessionRouter(
        repository=repository,
        gateway=turn_gateway,
        dedup=dedup,
        queue_depth=3,
        stall_window_seconds=30.0,
    )
    harness = Harness(
        gateway=None,  # type: ignore[arg-type]
        repository=repository,
        router=router,
        turn_gateway=turn_gateway,
        dedup=dedup,
        instance_id=instance_id,
    )

    def builder(
        adapter_cls: type[ChannelAdapter],
        *,
        instance_id: str = "",
        config: object = None,
        credentials: object = None,
    ) -> FakeAdapter:
        del adapter_cls, config, credentials
        adapter = FakeAdapter()
        if adapter_start_error is not None:

            async def _fail() -> None:
                raise adapter_start_error

            adapter.start = _fail  # type: ignore[method-assign]
        harness.adapters[instance_id] = adapter
        return adapter

    async def fake_sleep(delay: float) -> None:
        harness.sleeps.append(delay)
        # 永不返回：监督循环只做首次监督，之后完全由测试显式驱动
        await asyncio.Event().wait()

    harness.gateway = ChannelGateway(
        repository=repository,
        router=router,
        settings=ChannelSettings(
            enabled=enabled,
            credentials_dir=credentials_dir or str(tmp_path / "channel-credentials"),
            health_interval_seconds=HEALTH_INTERVAL,
        ),
        dedup=dedup,
        adapter_builder=builder,
        # 假渠道不在 ADAPTER_REGISTRY 里：注入解析器，避免污染全局注册表
        adapter_resolver=lambda channel: FakeAdapter if channel == "fake_bot" else None,
        worker_count=lambda: workers,
        clock=lambda: harness.now,
        sleep=fake_sleep,
        cleanup_interval_seconds=10_000.0,
        shutdown_turn_timeout=2.0,
    )
    return harness


# ==============================================================================
# 守卫三分支（框架设计 §7.1）
# ==============================================================================


@pytest.mark.asyncio
async def test_worker_count_missing_starts_with_single_process_assumption(
    tmp_path, caplog
) -> None:
    harness = _build(tmp_path, workers=None)
    with caplog.at_level(logging.WARNING):
        started = await harness.gateway.start()
    assert started is True
    assert harness.gateway.guard_reason is None
    assert "assuming a single worker process" in caplog.text
    await harness.settle()
    await harness.gateway.stop()
    assert harness.adapter.started == 1


@pytest.mark.asyncio
async def test_worker_count_not_one_refuses_to_connect_and_disables_instances(
    tmp_path,
) -> None:
    harness = _build(tmp_path, workers=4)
    started = await harness.gateway.start()

    assert started is False
    assert harness.gateway.guard_reason == "worker_count_not_one"
    assert harness.gateway.running is False
    assert harness.adapters == {}  # 没有建立任何连接
    record = harness.repository.get_instance(harness.instance_id)
    assert record is not None
    assert record.status == ChannelInstanceStatus.disabled.value


@pytest.mark.asyncio
async def test_insecure_credential_directory_refuses_to_connect_and_disables_instances(
    tmp_path,
) -> None:
    """凭据目录对 group/other 可读：**拒绝启动**，而不是带着已经泄露的凭据继续跑。"""
    credentials_dir = tmp_path / "channel-credentials"
    credentials_dir.mkdir(mode=0o755)
    harness = _build(tmp_path, workers=1, credentials_dir=str(credentials_dir))
    started = await harness.gateway.start()

    assert started is False
    assert harness.gateway.guard_reason == err.CHANNEL_CREDENTIAL_STORE_INSECURE
    assert harness.adapters == {}
    record = harness.repository.get_instance(harness.instance_id)
    assert record is not None
    assert record.status == ChannelInstanceStatus.disabled.value
    credentials: object = "not-read"
    with pytest.raises(DomainError) as excinfo:
        credentials = harness.gateway.credentials
    assert excinfo.value.code == err.CHANNEL_CREDENTIAL_STORE_UNAVAILABLE
    assert credentials == "not-read"


@pytest.mark.asyncio
async def test_credential_directory_is_created_owner_only(tmp_path) -> None:
    """目录不存在时由网关创建，权限必须是 0700。"""
    credentials_dir = tmp_path / "nested" / "channel-credentials"
    harness = _build(tmp_path, workers=1, credentials_dir=str(credentials_dir))

    assert await harness.gateway.start() is True

    assert (credentials_dir.stat().st_mode & 0o777) == 0o700
    await harness.gateway.stop()


@pytest.mark.asyncio
async def test_instance_without_usable_credentials_is_marked_error(
    tmp_path, caplog
) -> None:
    """引用指向的凭据文件不见了：实例落成 error，而不是拿空凭据反复重连。"""
    harness = _build(tmp_path, workers=1)
    assert await harness.gateway.start() is True
    # 模拟"文件被删掉"：实例行里留着一个指向不存在文件的引用
    harness.repository.update_instance(
        harness.instance_id, credential_ref="chan_" + "0" * 32
    )
    await harness.gateway.disconnect_instance(harness.instance_id)
    harness.adapters.clear()

    with caplog.at_level(logging.ERROR):
        await harness.gateway.connect_instance(harness.instance_id)

    assert harness.adapters == {}
    record = harness.repository.get_instance(harness.instance_id)
    assert record is not None
    assert record.status == ChannelInstanceStatus.error.value
    assert "is missing or empty" in caplog.text
    await harness.gateway.stop()


@pytest.mark.asyncio
async def test_disabled_instances_recover_on_the_next_successful_start(tmp_path) -> None:
    """守卫拒绝只是"这一次没起来"：修好配置重启后，实例必须自动回到可连接状态。"""
    refused = _build(tmp_path, workers=4)
    assert await refused.gateway.start() is False
    record = refused.repository.get_instance(refused.instance_id)
    assert record is not None
    assert record.status == ChannelInstanceStatus.disabled.value

    # 同一个库、修好配置后重启
    healthy = _build(tmp_path, workers=1)
    assert await healthy.gateway.start() is True
    await healthy.settle()
    await healthy.gateway.stop()

    recovered = healthy.repository.get_instance(healthy.instance_id)
    assert recovered is not None
    assert recovered.status == ChannelInstanceStatus.connected.value
    assert healthy.adapter.started == 1


@pytest.mark.asyncio
async def test_disabled_by_configuration_leaves_instance_status_untouched(
    tmp_path,
) -> None:
    harness = _build(tmp_path, enabled=False)
    started = await harness.gateway.start()

    assert started is False
    assert harness.gateway.guard_reason == "disabled_by_configuration"
    record = harness.repository.get_instance(harness.instance_id)
    assert record is not None
    assert record.status == ChannelInstanceStatus.pending.value


# ==============================================================================
# 装配与监督
# ==============================================================================


@pytest.mark.asyncio
async def test_start_connects_instances_and_registers_them(tmp_path) -> None:
    harness = _build(tmp_path)
    assert await harness.gateway.start() is True
    await harness.settle()
    await harness.gateway.stop()

    adapter = harness.adapter
    assert adapter.started == 1
    assert adapter.handler is not None  # 入站回调已注册
    record = harness.repository.get_instance(harness.instance_id)
    assert record is not None
    assert record.status == ChannelInstanceStatus.connected.value
    assert record.generation == 2  # 每次成功连接递增世代
    assert harness.gateway.is_connected(harness.instance_id) is False  # 已关闭


@pytest.mark.asyncio
async def test_unknown_channel_is_skipped_and_marked_error(tmp_path) -> None:
    harness = _build(tmp_path, instance_channel="not_a_channel")
    assert await harness.gateway.start() is True
    await harness.settle()

    assert harness.adapters == {}
    record = harness.repository.get_instance(harness.instance_id)
    assert record is not None
    assert record.status == ChannelInstanceStatus.error.value
    await harness.gateway.stop()


@pytest.mark.asyncio
async def test_connect_failure_follows_fixed_backoff_sequence(tmp_path) -> None:
    harness = _build(tmp_path, adapter_start_error=ConnectionError("boom"))
    # 不起监督循环：直接驱动监督，退避序列才可精确断言
    await harness.gateway._sync_instances()

    delays: list[float] = []
    for _ in range(len(BACKOFF_SEQUENCE_SECONDS) + 2):
        delays.append(await harness.gateway._supervise_once())
        harness.now += 100.0  # 跳过退避等待

    assert delays[: len(BACKOFF_SEQUENCE_SECONDS)] == list(BACKOFF_SEQUENCE_SECONDS)
    assert delays[-1] == BACKOFF_SEQUENCE_SECONDS[-1]  # 末项是封顶值
    record = harness.repository.get_instance(harness.instance_id)
    assert record is not None
    assert record.status == ChannelInstanceStatus.degraded.value


@pytest.mark.asyncio
async def test_backoff_wait_is_not_retried_before_it_is_due(tmp_path) -> None:
    harness = _build(tmp_path, adapter_start_error=ConnectionError("boom"))
    await harness.gateway._sync_instances()

    assert await harness.gateway._supervise_once() == BACKOFF_SEQUENCE_SECONDS[0]
    # 时间未推进：下一次监督只报告"还要等多久"，不重复尝试
    delay = await harness.gateway._supervise_once()
    assert delay == pytest.approx(BACKOFF_SEQUENCE_SECONDS[0])
    assert harness.adapter.started == 0


@pytest.mark.asyncio
async def test_dead_connection_is_reconnected_on_health_recheck(tmp_path) -> None:
    harness = _build(tmp_path)
    assert await harness.gateway.start() is True
    await harness.settle()
    adapter = harness.adapter
    assert adapter.started == 1

    adapter.stopped = adapter.started  # is_alive() -> False，模拟长连接已断
    await harness.gateway._supervise_once()
    assert adapter.started == 2
    await harness.gateway.stop()


@pytest.mark.asyncio
async def test_sync_disconnects_deleted_instance(tmp_path) -> None:
    harness = _build(tmp_path)
    assert await harness.gateway.start() is True
    await harness.settle()
    adapter = harness.adapter
    assert harness.gateway.supervised_instance_ids() == (harness.instance_id,)

    assert harness.repository.delete_instance(harness.instance_id) is True
    await harness.gateway.reload()

    assert harness.gateway.supervised_instance_ids() == ()
    assert adapter.stopped == 1
    await harness.gateway.stop()


@pytest.mark.asyncio
async def test_sync_picks_up_new_instance(tmp_path) -> None:
    harness = _build(tmp_path, with_instance=False)
    assert await harness.gateway.start() is True
    await harness.settle()
    assert harness.adapters == {}

    created = harness.repository.create_instance(channel="fake_bot", agent_id="agent-1")
    await harness.gateway.reload()
    await harness.gateway._supervise_once()

    assert harness.gateway.supervised_instance_ids() == (created.id,)
    assert harness.adapters[created.id].started == 1
    await harness.gateway.stop()


@pytest.mark.asyncio
async def test_connect_instance_is_idempotent(tmp_path) -> None:
    harness = _build(tmp_path)
    assert await harness.gateway.start() is True
    await harness.settle()

    assert await harness.gateway.connect_instance(harness.instance_id) is True
    assert await harness.gateway.connect_instance(harness.instance_id) is True
    await harness.gateway.stop()
    assert harness.adapter.started == 1


@pytest.mark.asyncio
async def test_reconnect_instance_rebuilds_the_connection(tmp_path) -> None:
    harness = _build(tmp_path)
    assert await harness.gateway.start() is True
    await harness.settle()

    assert await harness.gateway.reconnect_instance(harness.instance_id) is True
    await harness.gateway.stop()
    assert len(harness.adapters) == 1
    assert harness.adapter.started == 1
    assert harness.adapter.stopped == 1


# ==============================================================================
# 入站分发与出站
# ==============================================================================


@pytest.mark.asyncio
async def test_inbound_is_dispatched_through_the_router(tmp_path) -> None:
    harness = _build(tmp_path)
    assert await harness.gateway.start() is True
    await harness.settle()

    await harness.adapter.emit(
        InboundMessage(
            platform_event_id="evt-1",
            route=harness.route(),
            text="你好",
            received_at=datetime.now(UTC),
        )
    )
    assert await harness.idle()
    await harness.gateway.stop()

    assert harness.turn_gateway.turns == [("agent-1", "session-1", "你好")]
    assert harness.adapter.texts[0] == "收到，正在处理…"
    assert harness.adapter.texts[-1] == "最终结果"


@pytest.mark.asyncio
async def test_dedup_is_shared_with_the_router(tmp_path) -> None:
    harness = _build(tmp_path)
    assert await harness.gateway.start() is True
    await harness.settle()

    message = InboundMessage(
        platform_event_id="evt-dup",
        route=harness.route(),
        text="你好",
        received_at=datetime.now(UTC),
    )
    await harness.adapter.emit(message)
    await harness.adapter.emit(message)  # 平台重推：同一 (实例, 事件标识)
    assert await harness.idle()
    await harness.gateway.stop()

    assert len(harness.turn_gateway.turns) == 1
    assert harness.gateway.dedup is harness.dedup


@pytest.mark.asyncio
async def test_stop_waits_for_inflight_turn_before_disconnecting(tmp_path) -> None:
    harness = _build(tmp_path)
    harness.turn_gateway.default_script = FakeTurnScript(
        final_text="慢回合结果", terminal_delay=0.05
    )
    assert await harness.gateway.start() is True
    await harness.settle()

    await harness.send("你好", event_id="evt-slow")
    await asyncio.sleep(0)
    await harness.gateway.stop()

    adapter = harness.adapter
    # 回合结算后才断开：终稿已经送出，断开发生在它之后
    assert "慢回合结果" in adapter.texts
    assert adapter.stopped == 1
    assert await harness.router.wait_until_all_idle(timeout=0.1) is True


@pytest.mark.asyncio
async def test_connectivity_test_sends_fixed_text_without_a_turn(tmp_path) -> None:
    harness = _build(tmp_path)
    assert await harness.gateway.start() is True
    await harness.settle()

    result = await harness.gateway.send_test_message(
        instance_id=harness.instance_id,
        platform_user_id="u1",
        text="连通性测试",
    )
    await harness.gateway.stop()

    assert result.delivered
    assert harness.adapter.texts == ["连通性测试"]
    assert harness.turn_gateway.turns == []  # 不触发回合、不写会话历史
    deliveries = harness.repository.list_deliveries(instance_id=harness.instance_id)
    assert [item.certainty for item in deliveries] == ["delivered"]


@pytest.mark.asyncio
async def test_connectivity_test_on_unknown_instance_raises(tmp_path) -> None:
    harness = _build(tmp_path)
    assert await harness.gateway.start() is True
    await harness.settle()

    with pytest.raises(DomainError) as excinfo:
        await harness.gateway.send_test_message(
            instance_id="missing", platform_user_id="u1", text="连通性测试"
        )
    assert excinfo.value.code == err.CHANNEL_INSTANCE_NOT_FOUND
    await harness.gateway.stop()


@pytest.mark.asyncio
async def test_stop_disables_inbound_dispatch(tmp_path) -> None:
    harness = _build(tmp_path)
    assert await harness.gateway.start() is True
    await harness.settle()
    adapter = harness.adapter
    await harness.gateway.stop()

    # 关闭后到达的迟到回调被丢弃，不再进入管线
    await adapter.emit(
        InboundMessage(
            platform_event_id="evt-late",
            route=harness.route(),
            text="迟到的消息",
            received_at=datetime.now(UTC),
        )
    )
    assert harness.turn_gateway.turns == []


@pytest.mark.asyncio
async def test_periodic_cleanup_task_runs_and_stops(tmp_path) -> None:
    harness = _build(tmp_path)
    assert await harness.gateway.start() is True
    assert harness.gateway.dedup.retention_days >= 1
    await harness.gateway.stop()
    # 关闭后清理任务已取消（重复 stop 也是空操作）
    await harness.gateway.stop()
