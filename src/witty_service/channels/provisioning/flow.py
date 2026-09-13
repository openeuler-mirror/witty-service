"""接入编排：扫码接入的编排、落库、回滚，以及与实例装配的衔接（框架设计 §3.4）。

硬性约束（全部来自框架设计）：

1. **平台临时凭据只存服务端**：任何响应（`ProvisioningAttemptView`）都不含
   `state`，落库时加密；
2. **落库顺序固定"先凭据、后配置"**，任一步失败回滚，绝不留下"有配置没凭据"的
   半成品；
3. **同一渠道实例同时只允许一个进行中的接入尝试**（内存 + 数据库双重检查），
   重复发起返回既有尝试而不是报错；
4. 接入流程**不创建 agent、不创建会话、不发送任何消息**。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from witty_service.channels import errors as err
from witty_service.channels.adapters.base import (
    CredentialMaterial,
    resolve_adapter_class,
)
from witty_service.channels.contracts import ADAPTER_REGISTRY
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
from witty_service.persistence.channel_repository import (
    ChannelInstanceRecord,
    ChannelProvisioningRecord,
    ChannelRepository,
)
from witty_service.persistence.orm import ChannelInstanceStatus, ProvisioningStatus

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
    cipher: Any,
    channel: str,
    credentials: Mapping[str, str],
    owner_ref: str | None = None,
    agent_id: str | None = None,
    display_name: str | None = None,
) -> ChannelInstanceRecord:
    """**扫码与手填共用的落库路径**：先凭据、后配置，失败即回滚。

    回滚的含义是"删掉这次接入产生的实例行"——因此在失败时不会留下任何半成品，
    调用方看到的只有一条失败的接入尝试。
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

    # 第 1 步：先落凭据（密文 + 掩码），config 暂时为空
    instance = repository.create_instance(
        channel=channel,
        display_name=display_name,
        owner_ref=owner_ref,
        agent_id=agent_id,
        status=ChannelInstanceStatus.pending.value,
        config={},
        credential_ciphertext=cipher.encrypt_json(material.secrets),
        credential_mask=material.mask,
    )
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
        raise
    if updated is None:  # pragma: no cover - 刚创建的行不会消失
        repository.delete_instance(instance.id)
        raise err.channel_instance_not_found(instance.id)
    return updated


class ProvisioningFlow:
    """扫码接入编排。"""

    def __init__(
        self,
        *,
        repository: ChannelRepository,
        cipher: Any,
        driver_factory: DriverFactory | None = None,
        on_instance_ready: InstanceReadyHook | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._cipher = cipher
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
        attempt = self._repository.create_provisioning(
            channel=channel,
            owner_ref=owner_ref,
            agent_id=agent_id,
            status=ProvisioningStatus.waiting.value,
            qr_content=session.qr_content,
            poll_interval_ms=max(0, int(session.poll_interval_ms)),
            expires_at=session.expires_at,
            state_ciphertext=self._cipher.encrypt_json(
                {"state": session.state.decode("utf-8", errors="ignore")}
            ),
        )
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
                state_ciphertext=None,
                error_code=err.CHANNEL_PROVISIONING_EXPIRED,
            )
            self._forget(attempt)
            assert expired is not None
            return ProvisioningResult(attempt=self._view(expired), instance=None)

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
            updated = self._repository.update_provisioning(
                attempt.id,
                qr_content=outcome.qr_content or attempt.qr_content,
                expires_at=outcome.expires_at or attempt.expires_at,
            )
            assert updated is not None
            return ProvisioningResult(attempt=self._view(updated), instance=None)

        if outcome.status == STATUS_EXPIRED:
            expired = self._repository.update_provisioning(
                attempt.id,
                status=ProvisioningStatus.expired.value,
                state_ciphertext=None,
                error_code=outcome.error_code or err.CHANNEL_PROVISIONING_EXPIRED,
            )
            self._forget(attempt)
            assert expired is not None
            return ProvisioningResult(attempt=self._view(expired), instance=None)

        if outcome.status == STATUS_SUCCEEDED and outcome.credentials:
            return self._finish_succeeded(attempt, outcome)

        failed = self._repository.update_provisioning(
            attempt.id,
            status=ProvisioningStatus.failed.value,
            state_ciphertext=None,
            error_code=outcome.error_code or err.CHANNEL_PROVISIONING_FAILED,
        )
        self._forget(attempt)
        assert failed is not None
        return ProvisioningResult(attempt=self._view(failed), instance=None)

    def _finish_succeeded(
        self, attempt: ChannelProvisioningRecord, outcome: ProvisioningOutcome
    ) -> ProvisioningResult:
        credentials = dict(outcome.credentials or {})
        try:
            instance = persist_instance_from_credentials(
                repository=self._repository,
                cipher=self._cipher,
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
                state_ciphertext=None,
                error_code=getattr(exc, "code", err.CHANNEL_PROVISIONING_FAILED),
            )
            self._forget(attempt)
            assert failed is not None
            return ProvisioningResult(attempt=self._view(failed), instance=None)

        # 只有落库完全成功之后才把终态写回，并清除平台临时凭据
        settled = self._repository.update_provisioning(
            attempt.id,
            status=ProvisioningStatus.succeeded.value,
            state_ciphertext=None,
            error_code=None,
        )
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
        assert settled is not None
        return ProvisioningResult(
            attempt=self._view(settled, instance_id=instance.id), instance=instance
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
            state_ciphertext=None,
        )
        self._forget(attempt)
        assert cancelled is not None
        return self._view(cancelled)

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
            if attempt is not None and attempt.status == ProvisioningStatus.waiting.value:
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
        if attempt.state_ciphertext is None:
            return b""
        payload = self._cipher.decrypt_json(attempt.state_ciphertext)
        return str(payload.get("state", "")).encode("utf-8")

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
