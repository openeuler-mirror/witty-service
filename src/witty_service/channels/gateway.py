"""渠道网关：进程集成的连接监督与入站分发（框架设计 §3.3、§7.1、§7.2）。

职责边界：

- **worker 守卫**：进程数不为 1、或凭据加密密钥非法时**拒绝启动任何渠道连接**，
  并把实例状态写为 `disabled`——调用方能从实例列表区分"网关没起来"与
  "配置了但从未连上"（框架设计 §7.1）；
- **装配与监督**：按 `ADAPTER_REGISTRY` 构造适配器；未连接按固定退避序列重试，
  已连接按健康间隔复查（§3.3）；
- **入站分发**：适配器上报的事件一律交给 `SessionRouter` 的统一管线；
- **周期清理**：入站去重记录与已结束接入尝试的回收任务在启动时注册（§3.9、§7.2）；
- **优雅关闭**：停止接收新消息 -> 等待在飞回合（有上限）-> 断开连接。

本模块**不做密钥解析以外的事情**：密钥在 `start()` 里解析一次，失败即拒绝启动；
`__init__` 保持纯函数（无 IO），因为 `ServiceContainer.__post_init__` 会被大量测试
以 `MagicMock()` 依赖反复构造。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from witty_service.channels.adapters.base import (
    BaseChannelAdapter,
    resolve_adapter_class,
)
from witty_service.channels.contracts import (
    CONVERSATION_TYPE_DIRECT,
    ChannelAdapter,
    DeliveryResult,
    InboundMessage,
    Route,
)
from witty_service.channels.crypto import CredentialCipher
from witty_service.channels.dedup import (
    DEFAULT_RETENTION_DAYS,
    ChannelMaintenanceTask,
    InboundDedup,
)
from witty_service.channels.router import SessionRouter
from witty_service.config import ChannelSettings
from witty_service.domain.errors import DomainError
from witty_service.persistence.channel_repository import (
    ChannelInstanceRecord,
    ChannelRepository,
)
from witty_service.persistence.orm import ChannelInstanceStatus

logger = logging.getLogger(__name__)

#: 进程数环境变量：`cli.py` 在 `uvicorn.run` 之前写入（框架设计 §7.1）
WORKER_COUNT_ENV = "WITTY_WORKERS"

#: 固定退避序列，最后一项为封顶值（框架设计 §3.3）
BACKOFF_SEQUENCE_SECONDS: tuple[float, ...] = (0.25, 1.0, 3.0, 5.0, 10.0, 30.0)

#: 健康复查默认间隔（可由 `WITTY_CHANNEL_HEALTH_INTERVAL_SECONDS` 覆盖）
DEFAULT_HEALTH_INTERVAL_SECONDS = 15.0

#: 关闭时等待在飞回合的上限
DEFAULT_SHUTDOWN_TURN_TIMEOUT_SECONDS = 10.0

#: 监督循环的最小间隔：只用于避免忙循环
MIN_SUPERVISE_DELAY_SECONDS = 0.01

AdapterBuilder = Callable[..., ChannelAdapter]
#: 渠道标识符 -> 适配器类；缺省取 `ADAPTER_REGISTRY`（测试可注入假渠道）
AdapterResolver = Callable[[str], type[ChannelAdapter] | None]


def _default_adapter_builder(
    adapter_cls: type[BaseChannelAdapter],
    *,
    instance_id: str,
    config: Mapping[str, Any],
    credentials: Mapping[str, str],
) -> ChannelAdapter:
    return adapter_cls(
        instance_id=instance_id,
        config=config,
        credentials=credentials,
    )


def read_worker_count() -> int | None:
    """读取 `WITTY_WORKERS`；缺失或无法解析时返回 None（按单进程假设处理）。"""
    raw = os.getenv(WORKER_COUNT_ENV)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        logger.warning(
            "Cannot parse %s=%r; assuming a single worker process.",
            WORKER_COUNT_ENV,
            raw,
        )
        return None


@dataclass(slots=True)
class InstanceSupervision:
    """一个渠道实例的进程内监督状态。"""

    instance_id: str
    channel: str
    generation: int
    adapter: ChannelAdapter
    failures: int = 0
    next_attempt_at: float = 0.0
    connected: bool = False


@dataclass(slots=True)
class GatewaySnapshot:
    """给接口层看的只读快照（进程内事实，不落库）。"""

    started: bool = False
    reason: str | None = None
    supervised: tuple[str, ...] = field(default_factory=tuple)


class ChannelGateway:
    """进程内单例，随 FastAPI lifespan 启停（框架设计 §3.3）。"""

    def __init__(
        self,
        *,
        repository: ChannelRepository,
        router: SessionRouter,
        settings: ChannelSettings,
        dedup: InboundDedup | None = None,
        adapter_builder: AdapterBuilder | None = None,
        adapter_resolver: AdapterResolver | None = None,
        worker_count: Callable[[], int | None] | None = None,
        health_interval_seconds: float | None = None,
        backoff_seconds: Sequence[float] = BACKOFF_SEQUENCE_SECONDS,
        shutdown_turn_timeout: float = DEFAULT_SHUTDOWN_TURN_TIMEOUT_SECONDS,
        cleanup_interval_seconds: float | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._repository = repository
        self._router = router
        self._settings = settings
        self._adapter_builder: AdapterBuilder = adapter_builder or _default_adapter_builder
        self._adapter_resolver: AdapterResolver = adapter_resolver or resolve_adapter_class
        self._worker_count = worker_count or read_worker_count
        self._health_interval = max(
            MIN_SUPERVISE_DELAY_SECONDS,
            float(
                health_interval_seconds
                if health_interval_seconds is not None
                else settings.health_interval_seconds or DEFAULT_HEALTH_INTERVAL_SECONDS
            ),
        )
        self._backoff = tuple(backoff_seconds) or BACKOFF_SEQUENCE_SECONDS
        self._shutdown_turn_timeout = max(0.0, float(shutdown_turn_timeout))
        self._cleanup_interval = cleanup_interval_seconds
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep

        self._dedup = dedup or InboundDedup(
            repository,
            retention_days=settings.inbound_retention_days or DEFAULT_RETENTION_DAYS,
        )
        self._maintenance = ChannelMaintenanceTask(
            self._dedup,
            interval_seconds=(
                cleanup_interval_seconds
                if cleanup_interval_seconds is not None
                else 6 * 60 * 60
            ),
        )
        self._cipher: CredentialCipher | None = None
        self._instances: dict[str, InstanceSupervision] = {}
        self._supervisor: asyncio.Task[None] | None = None
        self._running = False
        self._accepting = False
        self._guard_reason: str | None = None

    # ==========================================================================
    # 只读快照
    # ==========================================================================

    @property
    def running(self) -> bool:
        return self._running

    @property
    def dedup(self) -> InboundDedup:
        return self._dedup

    @property
    def guard_reason(self) -> str | None:
        """拒绝启动的原因（`None` 表示没有拒绝）。"""
        return self._guard_reason

    @property
    def cipher(self) -> CredentialCipher:
        """凭据加密器；未成功启动时抛域错误（接口层据此返回明确错误码）。"""
        if self._cipher is None:
            raise self._cipher_unavailable()
        return self._cipher

    def snapshot(self) -> GatewaySnapshot:
        return GatewaySnapshot(
            started=self._running,
            reason=self._guard_reason,
            supervised=tuple(sorted(self._instances)),
        )

    def supervised_instance_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._instances))

    def is_connected(self, instance_id: str) -> bool:
        state = self._instances.get(instance_id)
        return state is not None and state.connected and _adapter_alive(state.adapter)

    # ==========================================================================
    # 生命周期
    # ==========================================================================

    async def start(self) -> bool:
        """启动网关。返回 False 表示**没有建立任何渠道连接**（守卫拒绝或总开关关闭）。"""
        if self._running:
            return True
        if not self._settings.enabled:
            logger.info(
                "Channel gateway is disabled by configuration "
                "(WITTY_CHANNEL_ENABLED=false); no channel connection is established."
            )
            self._guard_reason = "disabled_by_configuration"
            return False

        guard_error = self._guard()
        if guard_error is not None:
            self._guard_reason = guard_error
            self._disable_all_instances(guard_error)
            return False

        try:
            self._cipher = CredentialCipher.from_settings(self._settings.secret_key)
        except DomainError as exc:
            # 密钥缺失/非法：同样拒绝启动，并把实例状态写为 disabled（§7.1）
            logger.error(
                "Channel gateway refused to start: credential secret key is invalid "
                "(%s). No channel connection is established.",
                exc.code,
            )
            self._guard_reason = exc.code
            self._disable_all_instances(exc.code)
            return False

        # 上一次因守卫拒绝被置为 disabled 的实例，在成功启动时回到 pending 并重新连接：
        # `disabled` 只表达"网关没起来"，不是"这个实例被停用"（库里没有后者这个状态）。
        self._reset_disabled_instances()
        self._guard_reason = None
        self._running = True
        self._accepting = True
        self._maintenance.start()
        self._supervisor = asyncio.create_task(self._supervise())
        logger.info("Channel gateway started (health interval %.1fs)", self._health_interval)
        return True

    async def stop(self) -> None:
        """优雅关闭：停止接收 -> 等待在飞回合（有上限）-> 断开连接。"""
        self._accepting = False
        self._running = False
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None and not supervisor.done():
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor

        await self._router.wait_until_all_idle(timeout=self._shutdown_turn_timeout)

        for state in list(self._instances.values()):
            await self._stop_adapter(state)
        self._instances.clear()
        await self._maintenance.stop()
        self._cipher = None
        logger.info("Channel gateway stopped")

    # ==========================================================================
    # worker 守卫
    # ==========================================================================

    def _guard(self) -> str | None:
        workers = self._worker_count()
        if workers is None:
            logger.warning(
                "Cannot determine the worker count (%s is unset); assuming a single "
                "worker process and starting the channel gateway. Channel messages "
                "would be consumed twice if more than one worker is actually running.",
                WORKER_COUNT_ENV,
            )
            return None
        if workers != 1:
            logger.error(
                "Channel gateway refused to start: %s=%d. Channel messages would be "
                "consumed by every worker process. Run the service with a single "
                "worker (--workers 1).",
                WORKER_COUNT_ENV,
                workers,
            )
            return "worker_count_not_one"
        return None

    def _reset_disabled_instances(self) -> None:
        for record in self._repository.list_instances(
            status=ChannelInstanceStatus.disabled.value
        ):
            self._repository.update_instance(
                record.id, status=ChannelInstanceStatus.pending.value
            )

    def _disable_all_instances(self, reason: str) -> None:
        for record in self._repository.list_instances():
            if record.status == ChannelInstanceStatus.disabled.value:
                continue
            self._repository.update_instance(
                record.id, status=ChannelInstanceStatus.disabled.value
            )
        logger.warning(
            "All channel instances were marked as 'disabled' (reason=%s); "
            "no channel connection is established.",
            reason,
        )

    # ==========================================================================
    # 装配
    # ==========================================================================

    async def reload(self) -> None:
        """按数据库现状重新装配实例（新增接入 / 删除实例后调用）。"""
        if not self._running:
            return
        await self._sync_instances()

    async def connect_instance(self, instance_id: str) -> bool:
        """建立（或重建）单个实例的连接；返回是否已连接。"""
        record = self._repository.get_instance(instance_id)
        if record is None:
            return False
        state = self._instances.get(instance_id)
        if state is not None and state.connected and _adapter_alive(state.adapter):
            return True
        if state is not None:
            await self._stop_adapter(state)
            self._instances.pop(instance_id, None)
        built = self._build(record)
        if built is None:
            return False
        state = self._instances[instance_id] = built
        connected = await self._attempt_connect(state)
        return connected

    async def disconnect_instance(self, instance_id: str) -> None:
        state = self._instances.pop(instance_id, None)
        if state is not None:
            await self._stop_adapter(state)
            self._router.unregister_instance(instance_id)

    async def reconnect_instance(self, instance_id: str) -> bool:
        await self.disconnect_instance(instance_id)
        return await self.connect_instance(instance_id)

    async def send_test_message(
        self,
        *,
        instance_id: str,
        platform_user_id: str,
        conversation_type: str = CONVERSATION_TYPE_DIRECT,
        text: str,
    ) -> DeliveryResult:
        """连通性测试：只发固定文案，不提交回合、不写会话历史（框架设计 §9）。"""
        route = Route(
            instance_id=instance_id,
            conversation_type=conversation_type,
            platform_user_id=platform_user_id,
        )
        return await self._router.send_control_text(
            instance_id=instance_id, route=route, text=text
        )

    def _build(self, record: ChannelInstanceRecord) -> InstanceSupervision | None:
        adapter_cls = self._adapter_resolver(record.channel)
        if adapter_cls is None:
            logger.error(
                "Unknown channel '%s' on instance %s; skipping it. Known channels: %s",
                record.channel,
                record.id,
                ", ".join(sorted(_known_channels())) or "(none)",
            )
            self._repository.update_instance(
                record.id, status=ChannelInstanceStatus.error.value
            )
            return None
        credentials: Mapping[str, str] = {}
        if record.credential_ciphertext:
            try:
                credentials = self.cipher.decrypt_json(record.credential_ciphertext)
            except DomainError as exc:
                logger.error(
                    "Cannot decrypt credentials of channel instance %s (%s); "
                    "the instance needs to be provisioned again.",
                    record.id,
                    exc.code,
                )
                self._repository.update_instance(
                    record.id, status=ChannelInstanceStatus.error.value
                )
                return None
        adapter = self._adapter_builder(
            adapter_cls,
            instance_id=record.id,
            config=dict(record.config),
            credentials=dict(credentials),
        )
        adapter.on_inbound(self._handle_inbound)
        return InstanceSupervision(
            instance_id=record.id,
            channel=record.channel,
            generation=record.generation,
            adapter=adapter,
        )

    # ==========================================================================
    # 监督循环
    # ==========================================================================

    async def _supervise(self) -> None:
        while self._running:
            try:
                delay = await self._supervise_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Channel supervision tick failed")
                delay = self._health_interval
            try:
                await self._sleep(max(MIN_SUPERVISE_DELAY_SECONDS, delay))
            except asyncio.CancelledError:
                raise

    async def _supervise_once(self) -> float:
        """一次监督：返回下次唤醒的延迟。"""
        await self._sync_instances()
        delay = self._health_interval
        for state in list(self._instances.values()):
            if _adapter_alive(state.adapter):
                state.connected = True
                continue
            now = self._clock()
            if state.next_attempt_at > now:
                delay = min(delay, state.next_attempt_at - now)
                continue
            connected = await self._attempt_connect(state)
            if connected:
                continue
            backoff = self._backoff[min(state.failures, len(self._backoff) - 1)]
            state.failures += 1
            state.next_attempt_at = self._clock() + backoff
            delay = min(delay, backoff)
        return delay

    async def _sync_instances(self) -> None:
        """与数据库对齐：新增实例装配，已删除实例卸载。"""
        # 不按状态过滤：`disabled` 是"网关没起来"的临时标记，成功启动时已被重置
        records = {record.id: record for record in self._repository.list_instances()}
        for instance_id in list(self._instances):
            if instance_id not in records:
                logger.info(
                    "Channel instance %s is gone or disabled; disconnecting it.",
                    instance_id,
                )
                await self.disconnect_instance(instance_id)
        for instance_id, record in records.items():
            if instance_id in self._instances:
                continue
            built = self._build(record)
            if built is not None:
                self._instances[instance_id] = built

    async def _attempt_connect(self, state: InstanceSupervision) -> bool:
        try:
            await state.adapter.start()
        except asyncio.CancelledError:
            raise
        except Exception:
            state.connected = False
            logger.warning(
                "Channel adapter failed to start: instance_id=%s channel=%s "
                "attempt=%d",
                state.instance_id,
                state.channel,
                state.failures + 1,
                exc_info=True,
            )
            self._set_status(state.instance_id, ChannelInstanceStatus.degraded)
            return False
        state.connected = True
        state.failures = 0
        state.next_attempt_at = 0.0
        # 每次成功连接都递增世代：旧世代长连接的迟到回调会被管线丢弃
        updated = self._repository.update_instance(
            state.instance_id,
            status=ChannelInstanceStatus.connected.value,
            bump_generation=True,
        )
        if updated is not None:
            state.generation = updated.generation
        self._router.register_instance(
            state.instance_id,
            channel=state.channel,
            adapter=state.adapter,
            generation=state.generation,
            agent_id=None if updated is None else updated.agent_id,
        )
        logger.info(
            "Channel instance connected: instance_id=%s channel=%s",
            state.instance_id,
            state.channel,
        )
        return True

    async def _stop_adapter(self, state: InstanceSupervision) -> None:
        if not _adapter_alive(state.adapter):
            return
        state.connected = False
        try:
            await state.adapter.stop()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Channel adapter failed to stop cleanly: instance_id=%s",
                state.instance_id,
                exc_info=True,
            )

    def _set_status(self, instance_id: str, status: ChannelInstanceStatus) -> None:
        record = self._repository.get_instance(instance_id)
        if record is None or record.status == status.value:
            return
        self._repository.update_instance(instance_id, status=status.value)

    # ==========================================================================
    # 入站分发
    # ==========================================================================

    async def _handle_inbound(self, message: InboundMessage) -> None:
        if not self._accepting:
            logger.info(
                "Channel inbound dropped (gateway is not accepting): instance_id=%s",
                message.route.instance_id,
            )
            return
        try:
            await self._router.handle_inbound(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 单条消息的失败绝不影响连接与后续消息
            logger.exception(
                "Channel inbound handling failed: instance_id=%s",
                message.route.instance_id,
            )

    def _cipher_unavailable(self) -> DomainError:
        from witty_service.channels import errors as err

        return err.channel_secret_key_invalid(
            reason=self._guard_reason or "channel gateway is not running"
        )


def _adapter_alive(adapter: ChannelAdapter) -> bool:
    probe = getattr(adapter, "is_alive", None)
    if callable(probe):
        return bool(probe())
    return bool(getattr(adapter, "started", False))


def _known_channels() -> tuple[str, ...]:
    from witty_service.channels.contracts import registered_channels

    return registered_channels()


__all__ = [
    "BACKOFF_SEQUENCE_SECONDS",
    "DEFAULT_HEALTH_INTERVAL_SECONDS",
    "DEFAULT_SHUTDOWN_TURN_TIMEOUT_SECONDS",
    "WORKER_COUNT_ENV",
    "AdapterBuilder",
    "AdapterResolver",
    "ChannelGateway",
    "GatewaySnapshot",
    "InstanceSupervision",
    "read_worker_count",
]
