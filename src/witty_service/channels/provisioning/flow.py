"""接入编排：扫码接入的编排、落库、回滚，以及与实例装配的衔接（框架设计 §3.4）。

硬性约束（全部来自框架设计）：

1. **平台临时凭据只存服务端，且不入主库**：任何响应（`ProvisioningAttemptView`）
   都不含 `state`；它写在主库之外的 0600 凭据文件里，库里只有一个引用
2. **落库顺序固定"先凭据、后配置"**，任一步失败回滚（实例行与凭据文件一起删），
   绝不留下"有配置没凭据"或"有凭据没实例"的半成品；
3. **同一渠道实例同时只允许一个进行中的接入尝试**（内存 + 数据库双重检查），
   重复发起返回既有尝试而不是报错；
4. 接入流程**不创建 agent、不创建会话、不发送任何消息**。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from witty_service.channels import errors as err
from witty_service.channels.adapters.base import (
    CredentialMaterial,
    resolve_adapter_class,
)
from witty_service.channels.contracts import ADAPTER_REGISTRY
from witty_service.channels.credential_store import ChannelCredentialStore
from witty_service.channels.provisioning.drivers import (
    DEFAULT_POLL_INTERVAL_MS,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_WAITING,
    ProvisioningDriver,
    ProvisioningOutcome,
    get_driver_class,
)
from witty_service.domain.errors import DomainError
from witty_service.persistence.channel_repository import (
    ChannelInstanceRecord,
    ChannelProvisioningRecord,
    ChannelRepository,
)
from witty_service.persistence.orm import ChannelInstanceStatus, ProvisioningStatus

#: 平台临时凭据在凭据文件里的字段名：**base64**，因为 `ProvisioningSession.state`
#: 是任意字节，按 UTF-8 解码会静默损坏二进制状态
STATE_FIELD = "state_b64"

logger = logging.getLogger(__name__)

DriverFactory = Callable[[str], ProvisioningDriver]
InstanceReadyHook = Callable[[ChannelInstanceRecord], Coroutine[Any, Any, None]]


@dataclass(frozen=True, slots=True)
class ProvisioningAttemptView:
    """对外的接入尝试视图：**不含平台临时凭据**。"""

    attempt_id: str
    channel: str
    status: str
    qr_content: str | None
    expires_at: datetime
    poll_interval_ms: int
    error_code: str | None
    instance_id: str | None


@dataclass(frozen=True, slots=True)
class ProvisioningResult:
    attempt: ProvisioningAttemptView
    instance: ChannelInstanceRecord | None


def persist_instance_from_credentials(
    *,
    repository: ChannelRepository,
    store: ChannelCredentialStore,
    channel: str,
    credentials: Mapping[str, str],
    owner_ref: str | None = None,
    agent_id: str | None = None,
    display_name: str | None = None,
    instance_id: str | None = None,
) -> ChannelInstanceRecord:
    """**扫码与手填共用的落库路径**：先凭据文件、再实例行、最后非密配置。

    任一步失败都回滚：删掉这次接入产生的实例行**与凭据文件**。因此失败时不会留下
    任何半成品——既不会"有实例没凭据"，也不会"有凭据没实例"。

    凭据文件的引用由实例 id 派生（`ref_for_instance`），所以实例 id 必须在这里
    先生成，而不是交给仓储随手 uuid4。
    """
    adapter_cls = resolve_adapter_class(channel)
    if adapter_cls is None:
        raise err.channel_adapter_unknown(channel=channel)
    material: CredentialMaterial = adapter_cls.split_credentials(credentials)
    if not material.secrets:
        raise err.channel_credentials_invalid(
            channel=channel, reason="no secret fields were provided"
        )
    missing = [
        field
        for field in getattr(adapter_cls, "required_credentials", ())
        if not credentials.get(field)
    ]
    if missing:
        raise err.channel_credentials_invalid(
            channel=channel, reason=f"missing required fields: {', '.join(missing)}"
        )

    new_instance_id = instance_id or str(uuid4())
    credential_ref = store.ref_for_instance(new_instance_id)

    # 第 1 步：先落凭据（0600 文件），config 暂时为空
    store.write(credential_ref, material.secrets)
    try:
        instance = repository.create_instance(
            channel=channel,
            display_name=display_name,
            owner_ref=owner_ref,
            agent_id=agent_id,
            status=ChannelInstanceStatus.pending.value,
            config={},
            credential_ref=credential_ref,
            credential_mask=material.mask,
            instance_id=new_instance_id,
        )
    except Exception:
        # 实例行没落成：凭据文件不能留在磁盘上（引用由 uuid4 派生，不可能覆盖既有凭据）
        store.delete(credential_ref)
        raise
    # 第 2 步：再落非密配置；失败则回滚第 1 步
    try:
        updated = repository.update_instance(instance.id, config=material.config)
    except Exception:
        logger.error(
            "Channel instance config persistence failed; rolling back instance: "
            "instance_id=%s",
            instance.id,
            exc_info=True,
        )
        repository.delete_instance(instance.id)
        store.delete(credential_ref)
        raise
    if updated is None:  # pragma: no cover - 刚创建的行不会消失
        repository.delete_instance(instance.id)
        store.delete(credential_ref)
        raise err.channel_instance_not_found(instance.id)
    return updated


class ProvisioningFlow:
    """扫码接入编排。"""

    def __init__(
        self,
        *,
        repository: ChannelRepository,
        store: ChannelCredentialStore,
        driver_factory: DriverFactory | None = None,
        on_instance_ready: InstanceReadyHook | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._store = store
        self._driver_factory = driver_factory or _default_driver_factory
        self._on_instance_ready = on_instance_ready
        self._clock = clock or (lambda: datetime.now(UTC))
        #: (channel, owner_ref) -> attempt_id：同一实例只允许一个进行中的接入尝试
        self._inflight: dict[tuple[str, str | None], str] = {}
        self._last_poll_at: dict[str, datetime] = {}

    # ==========================================================================
    # 开始
    # ==========================================================================

    async def begin(
        self,
        *,
        channel: str,
        owner_ref: str | None = None,
        agent_id: str | None = None,
    ) -> ProvisioningAttemptView:
        self._require_channel(channel)
        existing = self._find_inflight(channel, owner_ref)
        if existing is not None:
            # 重复发起返回既有尝试，而不是报错（同一实例同时只允许一个尝试）
            return self._view(existing)

        # 顺带回收已过期的平台临时凭据：这些文件不由数据库的保留期管理
        # （接入尝试被删除后它们就是孤儿），所以每次发起新接入时清一次。
        self._store.prune_expired(now=self._clock())

        driver = self._driver_factory(channel)
        try:
            session = await driver.begin()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 驱动异常不得冒泡成 500：平台不可达/协议不符是**可预期的运营事件**
            logger.warning(
                "Channel provisioning driver failed to begin: channel=%s",
                channel,
                exc_info=True,
            )
            raise err.channel_provisioning_unavailable(
                channel=channel, reason=str(exc) or exc.__class__.__name__
            ) from exc
        # 平台临时凭据：先在主库之外落成 0600 文件，库里只留引用
        attempt_id = str(uuid4())
        state_ref = self._store.ref_for_provisioning(attempt_id)
        self._store.write(
            state_ref,
            {STATE_FIELD: base64.b64encode(session.state).decode("ascii")},
            expires_at=session.expires_at,
        )
        try:
            attempt = self._repository.create_provisioning(
                channel=channel,
                owner_ref=owner_ref,
                agent_id=agent_id,
                status=ProvisioningStatus.waiting.value,
                qr_content=session.qr_content,
                poll_interval_ms=max(0, int(session.poll_interval_ms)),
                expires_at=session.expires_at,
                state_ref=state_ref,
                attempt_id=attempt_id,
            )
        except Exception:
            self._store.delete(state_ref)
            raise
        self._inflight[(channel, owner_ref)] = attempt.id
        return self._view(attempt)

    # ==========================================================================
    # 轮询
    # ==========================================================================

    async def poll(self, attempt_id: str, *, force: bool = False) -> ProvisioningResult:
        attempt = self._require_attempt(attempt_id)

        if attempt.status != ProvisioningStatus.waiting.value:
            return ProvisioningResult(attempt=self._view(attempt), instance=None)

        now = self._clock()
        if self._is_expired(attempt, now):
            expired = self._repository.update_provisioning(
                attempt.id,
                status=ProvisioningStatus.expired.value,
                state_ref=None,
                error_code=err.CHANNEL_PROVISIONING_EXPIRED,
            )
            self._clear_state(attempt)
            self._forget(attempt)
            return ProvisioningResult(
                attempt=self._view(_require(expired, attempt.id)), instance=None
            )

        if not force and self._throttled(attempt, now):
            return ProvisioningResult(attempt=self._view(attempt), instance=None)
        self._last_poll_at[attempt.id] = now

        try:
            outcome = await self._driver_factory(attempt.channel).poll(
                self._decode_state(attempt)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # 轮询失败是"这次尝试失败"，不是接口错误：写回终态，前端据此停止轮询
            logger.warning(
                "Channel provisioning poll failed: attempt_id=%s channel=%s",
                attempt.id,
                attempt.channel,
                exc_info=True,
            )
            outcome = ProvisioningOutcome(
                status=STATUS_FAILED, error_code=err.CHANNEL_PROVISIONING_FAILED
            )
        return self._apply_outcome(attempt, outcome)

    def _apply_outcome(
        self, attempt: ChannelProvisioningRecord, outcome: ProvisioningOutcome
    ) -> ProvisioningResult:
        if outcome.status == STATUS_WAITING:
            if not self._rotate_state(attempt, outcome):
                return self._fail(attempt, err.CHANNEL_PROVISIONING_FAILED)
            updated = self._repository.update_provisioning(
                attempt.id,
                qr_content=outcome.qr_content or attempt.qr_content,
                expires_at=outcome.expires_at or attempt.expires_at,
            )
            return ProvisioningResult(
                attempt=self._view(_require(updated, attempt.id)), instance=None
            )

        if outcome.status == STATUS_EXPIRED:
            expired = self._repository.update_provisioning(
                attempt.id,
                status=ProvisioningStatus.expired.value,
                state_ref=None,
                error_code=outcome.error_code or err.CHANNEL_PROVISIONING_EXPIRED,
            )
            self._clear_state(attempt)
            self._forget(attempt)
            return ProvisioningResult(
                attempt=self._view(_require(expired, attempt.id)), instance=None
            )

        if outcome.status == STATUS_SUCCEEDED and outcome.credentials:
            return self._finish_succeeded(attempt, outcome)

        return self._fail(
            attempt, outcome.error_code or err.CHANNEL_PROVISIONING_FAILED
        )

    def _fail(
        self, attempt: ChannelProvisioningRecord, error_code: str
    ) -> ProvisioningResult:
        """把尝试写成失败终态，并清掉平台临时凭据。"""
        failed = self._repository.update_provisioning(
            attempt.id,
            status=ProvisioningStatus.failed.value,
            state_ref=None,
            error_code=error_code,
        )
        self._clear_state(attempt)
        self._forget(attempt)
        return ProvisioningResult(
            attempt=self._view(_require(failed, attempt.id)), instance=None
        )

    def _rotate_state(
        self, attempt: ChannelProvisioningRecord, outcome: ProvisioningOutcome
    ) -> bool:
        """平台换了会话：把新的临时凭据写回同一个状态文件。

        顺序有讲究——先落状态、再回二维码：反过来客户端可能拿着刚拿到的新二维码来轮询，
        而驱动手里还是那个已经作废的旧会话，新二维码永远扫不出结果。写状态失败（磁盘满、
        权限被改）时返回 False——平台会话已不可用，只能让这次接入失败。
        """
        if outcome.state is None or attempt.state_ref is None:
            return True
        try:
            self._store.write(
                attempt.state_ref,
                {STATE_FIELD: base64.b64encode(outcome.state).decode("ascii")},
                expires_at=outcome.expires_at or attempt.expires_at,
            )
        except Exception:
            logger.warning(
                "Failed to persist the rotated platform session: attempt_id=%s "
                "channel=%s",
                attempt.id,
                attempt.channel,
                exc_info=True,
            )
            return False
        return True

    def _finish_succeeded(
        self, attempt: ChannelProvisioningRecord, outcome: ProvisioningOutcome
    ) -> ProvisioningResult:
        credentials = dict(outcome.credentials or {})
        try:
            instance = persist_instance_from_credentials(
                repository=self._repository,
                store=self._store,
                channel=attempt.channel,
                credentials=credentials,
                owner_ref=attempt.owner_ref,
                agent_id=attempt.agent_id,
            )
        except Exception as exc:
            logger.error(
                "Channel provisioning persistence failed: attempt_id=%s",
                attempt.id,
                exc_info=True,
            )
            failed = self._repository.update_provisioning(
                attempt.id,
                status=ProvisioningStatus.failed.value,
                state_ref=None,
                error_code=getattr(exc, "code", err.CHANNEL_PROVISIONING_FAILED),
            )
            self._clear_state(attempt)
            self._forget(attempt)
            return ProvisioningResult(
                attempt=self._view(_require(failed, attempt.id)), instance=None
            )

        # 只有落库完全成功之后才把终态写回，并清除平台临时凭据
        settled = self._repository.update_provisioning(
            attempt.id,
            status=ProvisioningStatus.succeeded.value,
            state_ref=None,
            error_code=None,
        )
        self._clear_state(attempt)
        self._forget(attempt)
        if self._on_instance_ready is not None:
            # 通知网关装配并连接；失败不影响"接入已成功"这一事实
            try:
                _schedule(self._on_instance_ready(instance))
            except Exception:
                logger.warning(
                    "Failed to notify channel gateway about new instance: %s",
                    instance.id,
                    exc_info=True,
                )
        return ProvisioningResult(
            attempt=self._view(_require(settled, attempt.id), instance_id=instance.id),
            instance=instance,
        )

    # ==========================================================================
    # 取消
    # ==========================================================================

    async def cancel(self, attempt_id: str) -> ProvisioningAttemptView:
        attempt = self._require_attempt(attempt_id)
        if attempt.status != ProvisioningStatus.waiting.value:
            return self._view(attempt)
        cancelled = self._repository.update_provisioning(
            attempt.id,
            status=ProvisioningStatus.cancelled.value,
            state_ref=None,
        )
        self._clear_state(attempt)
        self._forget(attempt)
        return self._view(_require(cancelled, attempt.id))

    # ==========================================================================
    # 内部
    # ==========================================================================

    def _require_channel(self, channel: str) -> None:
        if channel not in ADAPTER_REGISTRY:
            raise err.channel_adapter_unknown(channel=channel)
        if get_driver_class(channel) is None:
            raise err.channel_adapter_unknown(channel=channel)

    def _require_attempt(self, attempt_id: str) -> ChannelProvisioningRecord:
        attempt = self._repository.get_provisioning(attempt_id)
        if attempt is None:
            raise err.channel_provisioning_not_found(attempt_id)
        return attempt

    def _find_inflight(
        self, channel: str, owner_ref: str | None
    ) -> ChannelProvisioningRecord | None:
        key = (channel, owner_ref)
        attempt_id = self._inflight.get(key)
        if attempt_id is not None:
            attempt = self._repository.get_provisioning(attempt_id)
            if (
                attempt is not None
                and attempt.status == ProvisioningStatus.waiting.value
            ):
                return attempt
            self._inflight.pop(key, None)
        # 数据库侧检查：进程重启后内存登记会丢，双保险
        attempt = self._repository.find_waiting_provisioning(
            channel=channel, owner_ref=owner_ref, now=self._clock()
        )
        if attempt is not None:
            self._inflight[key] = attempt.id
        return attempt

    def _is_expired(self, attempt: ChannelProvisioningRecord, now: datetime) -> bool:
        expires_at = attempt.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return expires_at <= now

    def _throttled(self, attempt: ChannelProvisioningRecord, now: datetime) -> bool:
        interval_ms = int(attempt.poll_interval_ms or 0)
        if interval_ms <= 0:
            return False
        last = self._last_poll_at.get(attempt.id)
        if last is None:
            return False
        return (now - last).total_seconds() * 1000 < interval_ms

    def _decode_state(self, attempt: ChannelProvisioningRecord) -> bytes:
        """取回平台临时凭据（`begin()` 时落在 0600 文件里的那份）。"""
        if attempt.state_ref is None:
            return b""
        payload = self._store.resolve(attempt.state_ref)
        if not payload:
            # 文件被判为不可信（权限过宽）或已被清理：当作"没有状态"，让驱动自己失败
            raise err.channel_credentials_invalid(
                channel=attempt.channel,
                reason="the platform session state is missing or not usable",
            )
        try:
            return base64.b64decode(str(payload.get(STATE_FIELD, "")), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise err.channel_credentials_invalid(
                channel=attempt.channel,
                reason="the platform session state is corrupted",
            ) from exc

    def _clear_state(self, attempt: ChannelProvisioningRecord) -> None:
        """删除平台临时凭据文件（成功/失败/取消/过期都会走到这里）。

        删除失败只记日志：接入尝试的终态已经写好了，不该因为一个残留文件而回滚。
        残留文件由 `begin()` 里的 `prune_expired` 兜底回收。
        """
        if attempt.state_ref is None:
            return
        try:
            self._store.delete(attempt.state_ref)
        except DomainError:
            logger.warning(
                "Failed to delete the provisioning credential file: attempt_id=%s ref=%s",
                attempt.id,
                attempt.state_ref,
                exc_info=True,
            )

    def _forget(self, attempt: ChannelProvisioningRecord) -> None:
        self._inflight.pop((attempt.channel, attempt.owner_ref), None)
        self._last_poll_at.pop(attempt.id, None)

    def _view(
        self, attempt: ChannelProvisioningRecord, *, instance_id: str | None = None
    ) -> ProvisioningAttemptView:
        return ProvisioningAttemptView(
            attempt_id=attempt.id,
            channel=attempt.channel,
            status=attempt.status,
            qr_content=attempt.qr_content,
            # SQLite 不保存时区：对外统一按 UTC 呈现，避免调用方拿到裸时间
            expires_at=_as_utc(attempt.expires_at),
            poll_interval_ms=int(attempt.poll_interval_ms),
            error_code=attempt.error_code,
            instance_id=instance_id,
        )


def _require(
    record: ChannelProvisioningRecord | None, attempt_id: str
) -> ChannelProvisioningRecord:
    """把"更新后拿不到记录"变成域错误。

    原先的 `assert ... is not None` 在 `python -O` 下会被整条剥掉（bandit B101），届时
    同一路径退化成一个 `None` 往下传，在别处炸成 `AttributeError`。
    """
    if record is None:
        raise err.channel_provisioning_not_found(attempt_id)
    return record


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _default_driver_factory(channel: str) -> ProvisioningDriver:
    driver_cls = get_driver_class(channel)
    if driver_cls is None:
        raise err.channel_adapter_unknown(channel=channel)
    return driver_cls()


def _schedule(coro: Coroutine[Any, Any, None]) -> None:
    """在事件循环内调度通知；没有运行中的循环时退化为直接丢弃（只记日志）。"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # pragma: no cover - 同步调用场景
        coro.close()
        logger.warning("No running loop to notify channel gateway about new instance")
        return
    task: asyncio.Task[None] = loop.create_task(coro)
    task.add_done_callback(_log_task_failure)


def _log_task_failure(task: Any) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("Channel instance-ready hook failed", exc_info=exc)


__all__ = [
    "DEFAULT_POLL_INTERVAL_MS",
    "ProvisioningAttemptView",
    "ProvisioningFlow",
    "ProvisioningResult",
    "persist_instance_from_credentials",
]
