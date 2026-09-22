"""W13 接入编排的测试：覆盖 9.1 第 1–4 条（接入的开始 / 成功 / 过期 / 取消与手填）。"""

from __future__ import annotations

import base64
from dataclasses import fields
from datetime import UTC, datetime, timedelta

import pytest

from witty_service.channels import errors as err
from witty_service.channels.contracts import registered_channels
from witty_service.channels.credential_store import ChannelCredentialStore
from witty_service.channels.provisioning import drivers as driver_module
from witty_service.channels.provisioning.drivers import (
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_WAITING,
    ProvisioningOutcome,
    ProvisioningSession,
)
from witty_service.channels.provisioning.flow import (
    STATE_FIELD,
    ProvisioningAttemptView,
    ProvisioningFlow,
)
from witty_service.channels.provisioning.manual import ManualCredentialBinder
from witty_service.domain.errors import DomainError
from witty_service.persistence.channel_repository import ChannelRepository
from witty_service.persistence.db import create_session_factory, create_sqlite_engine
from witty_service.persistence.orm import Base

INSTANCE_LESS_STATE = b"platform-temp-state"


def _now() -> datetime:
    return datetime.now(UTC)


class FakeDriver:
    """假接入驱动：按脚本给出开始结果与轮询结果。"""

    channel = "wecom_bot"

    def __init__(
        self,
        *,
        session: ProvisioningSession | None = None,
        outcomes: list[ProvisioningOutcome] | None = None,
        begin_error: BaseException | None = None,
        poll_error: BaseException | None = None,
    ) -> None:
        self.session = session or ProvisioningSession(
            qr_content="https://example.test/qr",
            expires_at=_now() + timedelta(minutes=5),
            poll_interval_ms=0,
            state=INSTANCE_LESS_STATE,
        )
        self.outcomes = list(outcomes or [])
        self.begin_error = begin_error
        self.poll_error = poll_error
        self.begin_calls = 0
        self.poll_calls: list[bytes] = []

    async def begin(self) -> ProvisioningSession:
        self.begin_calls += 1
        if self.begin_error is not None:
            raise self.begin_error
        return self.session

    async def poll(self, state: bytes) -> ProvisioningOutcome:
        self.poll_calls.append(state)
        if self.poll_error is not None:
            raise self.poll_error
        if self.outcomes:
            return self.outcomes.pop(0)
        return ProvisioningOutcome(status=STATUS_WAITING)


@pytest.fixture
def repository(tmp_path) -> ChannelRepository:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'provisioning.sqlite3'}")
    Base.metadata.create_all(engine)
    yield ChannelRepository(create_session_factory(engine))
    engine.dispose()


@pytest.fixture
def store(tmp_path) -> ChannelCredentialStore:
    return ChannelCredentialStore(tmp_path / "channel-credentials")


def _flow(
    repository: ChannelRepository,
    store: ChannelCredentialStore,
    driver: FakeDriver,
    *,
    ready: list[str] | None = None,
) -> ProvisioningFlow:
    async def _on_ready(instance):  # type: ignore[no-untyped-def]
        if ready is not None:
            ready.append(instance.id)

    return ProvisioningFlow(
        repository=repository,
        store=store,
        driver_factory=lambda _channel: driver,
        on_instance_ready=_on_ready if ready is not None else None,
    )


# ==============================================================================
# 9.1 第 1 条：接入开始
# ==============================================================================


@pytest.mark.asyncio
async def test_begin_returns_qr_without_state(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    driver = FakeDriver()
    flow = _flow(repository, store, driver)

    view = await flow.begin(
        channel="wecom_bot", owner_ref="owner-1", agent_id="agent-1"
    )

    assert view.status == STATUS_WAITING
    assert view.qr_content == "https://example.test/qr"
    assert view.attempt_id
    assert view.expires_at > _now()
    # 响应结构里根本没有 state 字段（平台临时凭据只存服务端）
    assert {field.name for field in fields(ProvisioningAttemptView)} == {
        "attempt_id",
        "channel",
        "status",
        "qr_content",
        "expires_at",
        "poll_interval_ms",
        "error_code",
        "instance_id",
    }
    stored = repository.get_provisioning(view.attempt_id)
    assert stored is not None
    # 库里只有一个引用；平台临时凭据本体在 0600 文件里
    assert stored.state_ref is not None
    payload = store.resolve(stored.state_ref)
    assert payload is not None
    assert base64.b64decode(payload[STATE_FIELD]) == INSTANCE_LESS_STATE


@pytest.mark.asyncio
async def test_begin_does_not_create_agent_or_session(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    """接入流程不创建 agent、不创建会话、不发送任何消息（特性设计文档 1.4）。"""
    flow = _flow(repository, store, FakeDriver())

    await flow.begin(channel="wecom_bot", owner_ref="o", agent_id="agent-1")

    assert repository.list_instances() == []


@pytest.mark.asyncio
async def test_duplicate_begin_returns_existing_attempt(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    """同一实例同时只允许一个进行中的接入尝试：重复发起返回既有尝试而不是报错。"""
    driver = FakeDriver()
    flow = _flow(repository, store, driver)

    first = await flow.begin(channel="wecom_bot", owner_ref="owner-1")
    second = await flow.begin(channel="wecom_bot", owner_ref="owner-1")

    assert first.attempt_id == second.attempt_id
    assert driver.begin_calls == 1


@pytest.mark.asyncio
async def test_other_owner_can_provision_concurrently(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    flow = _flow(repository, store, FakeDriver())

    first = await flow.begin(channel="wecom_bot", owner_ref="owner-1")
    second = await flow.begin(channel="wecom_bot", owner_ref="owner-2")

    assert first.attempt_id != second.attempt_id


@pytest.mark.asyncio
async def test_unknown_channel_is_rejected(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    flow = _flow(repository, store, FakeDriver())

    with pytest.raises(DomainError) as excinfo:
        await flow.begin(channel="nope_bot")

    assert excinfo.value.code == err.CHANNEL_ADAPTER_UNKNOWN


# ==============================================================================
# 9.1 第 2 条：接入成功
# ==============================================================================


@pytest.mark.asyncio
async def test_poll_succeeded_persists_credentials_and_creates_instance(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    driver = FakeDriver(
        outcomes=[
            ProvisioningOutcome(status=STATUS_WAITING),
            ProvisioningOutcome(
                status=STATUS_SUCCEEDED,
                credentials={"bot_id": "bot-12345678", "secret": "s3cr3t"},
            ),
        ]
    )
    ready: list[str] = []
    flow = _flow(repository, store, driver, ready=ready)
    view = await flow.begin(
        channel="wecom_bot", owner_ref="owner-1", agent_id="agent-1"
    )

    first = await flow.poll(view.attempt_id)
    assert first.attempt.status == STATUS_WAITING

    second = await flow.poll(view.attempt_id)

    assert second.attempt.status == STATUS_SUCCEEDED
    assert second.instance is not None
    instance = second.instance
    # 对外只有掩码（首 4 末 4），密文字段不进 config
    assert instance.credential_mask == "bot-****5678"
    assert instance.config == {"bot_id": "bot-12345678"}
    assert instance.credential_ref is not None
    assert store.resolve(instance.credential_ref) == {"secret": "s3cr3t"}
    # 平台临时凭据已清除：引用置空，文件也删掉
    stored = repository.get_provisioning(view.attempt_id)
    assert stored is not None
    assert stored.state_ref is None
    assert store.resolve(store.ref_for_provisioning(view.attempt_id)) is None
    assert stored.status == STATUS_SUCCEEDED
    # 编排衔接到实例装配
    import asyncio

    await asyncio.sleep(0)
    assert ready == [instance.id]


@pytest.mark.asyncio
async def test_poll_throttles_by_poll_interval(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    driver = FakeDriver(
        session=ProvisioningSession(
            qr_content="qr",
            expires_at=_now() + timedelta(minutes=5),
            poll_interval_ms=60_000,
            state=INSTANCE_LESS_STATE,
        )
    )
    flow = _flow(repository, store, driver)
    view = await flow.begin(channel="wecom_bot", owner_ref="owner-1")

    await flow.poll(view.attempt_id)
    await flow.poll(view.attempt_id)

    assert len(driver.poll_calls) == 1


@pytest.mark.asyncio
async def test_poll_after_terminal_state_is_read_only(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    driver = FakeDriver(
        outcomes=[
            ProvisioningOutcome(
                status=STATUS_SUCCEEDED,
                # 必须给全适配器声明的必填凭据（`required_credentials`），否则落库会失败
                credentials={"bot_id": "bot-12345678", "secret": "s3cr3t"},
            )
        ]
    )
    flow = _flow(repository, store, driver)
    view = await flow.begin(channel="wecom_bot", owner_ref="owner-1")
    await flow.poll(view.attempt_id)

    again = await flow.poll(view.attempt_id)

    assert again.attempt.status == STATUS_SUCCEEDED
    assert len(driver.poll_calls) == 1


# ==============================================================================
# 驱动异常：平台不可达 / 协议不符不得变成 500
# ==============================================================================


@pytest.mark.asyncio
async def test_begin_driver_failure_raises_domain_error(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    """驱动无法开始：抛域错误（502），且库里**不留下**任何接入尝试。"""
    driver = FakeDriver(begin_error=RuntimeError("platform unreachable"))
    flow = _flow(repository, store, driver)

    with pytest.raises(DomainError) as excinfo:
        await flow.begin(channel="wecom_bot", owner_ref="owner-1")

    assert excinfo.value.code == err.CHANNEL_PROVISIONING_FAILED
    assert excinfo.value.status_code == 502
    assert "platform unreachable" in str(excinfo.value.details)
    assert repository.list_instances() == []


@pytest.mark.asyncio
async def test_poll_driver_failure_marks_attempt_failed(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    """轮询失败是"这次尝试失败"，不是接口错误：写回终态，前端据此停止轮询。"""
    driver = FakeDriver(poll_error=RuntimeError("platform unreachable"))
    flow = _flow(repository, store, driver)
    view = await flow.begin(channel="wecom_bot", owner_ref="owner-1")

    result = await flow.poll(view.attempt_id)

    assert result.attempt.status == STATUS_FAILED
    assert result.attempt.error_code == err.CHANNEL_PROVISIONING_FAILED
    assert result.instance is None

    # 终态之后不再触碰平台
    again = await flow.poll(view.attempt_id)
    assert again.attempt.status == STATUS_FAILED
    assert len(driver.poll_calls) == 1


@pytest.mark.asyncio
async def test_begin_failure_allows_retry(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    """开始失败不占用"进行中"名额：修好平台后可以立刻重试。"""
    failing = FakeDriver(begin_error=RuntimeError("boom"))
    flow = _flow(repository, store, failing)
    with pytest.raises(DomainError):
        await flow.begin(channel="wecom_bot", owner_ref="owner-1")

    working = FakeDriver()
    flow_ok = _flow(repository, store, working)
    view = await flow_ok.begin(channel="wecom_bot", owner_ref="owner-1")

    assert view.status == STATUS_WAITING
    assert working.begin_calls == 1


@pytest.mark.asyncio
async def test_poll_unknown_attempt_raises(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    flow = _flow(repository, store, FakeDriver())

    with pytest.raises(DomainError) as excinfo:
        await flow.poll("missing")

    assert excinfo.value.code == err.CHANNEL_PROVISIONING_NOT_FOUND


@pytest.mark.asyncio
async def test_succeeded_without_credentials_marks_failed(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    driver = FakeDriver(outcomes=[ProvisioningOutcome(status=STATUS_SUCCEEDED)])
    flow = _flow(repository, store, driver)
    view = await flow.begin(channel="wecom_bot", owner_ref="o")

    result = await flow.poll(view.attempt_id)

    assert result.attempt.status == STATUS_FAILED
    assert repository.list_instances() == []


@pytest.mark.asyncio
async def test_failed_outcome_clears_state(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    driver = FakeDriver(
        outcomes=[ProvisioningOutcome(status=STATUS_FAILED, error_code="WECOM_NOPE")]
    )
    flow = _flow(repository, store, driver)
    view = await flow.begin(channel="wecom_bot", owner_ref="o")

    result = await flow.poll(view.attempt_id)

    assert result.attempt.status == STATUS_FAILED
    assert result.attempt.error_code == "WECOM_NOPE"
    stored = repository.get_provisioning(view.attempt_id)
    assert stored is not None
    assert stored.state_ref is None
    assert store.resolve(store.ref_for_provisioning(view.attempt_id)) is None


@pytest.mark.asyncio
async def test_waiting_outcome_may_refresh_qr(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    new_expiry = _now() + timedelta(minutes=10)
    driver = FakeDriver(
        outcomes=[
            ProvisioningOutcome(
                status=STATUS_WAITING, qr_content="qr-2", expires_at=new_expiry
            )
        ]
    )
    flow = _flow(repository, store, driver)
    view = await flow.begin(channel="wecom_bot", owner_ref="o")

    result = await flow.poll(view.attempt_id)

    assert result.attempt.qr_content == "qr-2"
    assert result.attempt.expires_at == new_expiry


@pytest.mark.asyncio
async def test_waiting_outcome_may_rotate_the_platform_session(
    repository: ChannelRepository, store: ChannelCredentialStore, monkeypatch
) -> None:
    """平台换会话时必须把新 state 落回同一个文件，且先落状态再回二维码。

    反序的话客户端可能拿着新二维码来轮询，而驱动手里还是已作废的旧会话（见 `_rotate_state`）。
    """
    rotated = b'{"task_id":"TASK_2"}'
    driver = FakeDriver(
        outcomes=[
            ProvisioningOutcome(
                status=STATUS_WAITING,
                qr_content="qr-2",
                expires_at=_now() + timedelta(minutes=5),
                state=rotated,
            )
        ]
    )
    flow = _flow(repository, store, driver)
    view = await flow.begin(channel="wecom_bot", owner_ref="o")
    # 在"回二维码"这一刻回读状态文件：这是唯一能验证先后顺序的观测点
    observed: list[bytes | None] = []
    original_update = repository.update_provisioning

    def _observe(attempt_id: str, **fields: object):  # type: ignore[no-untyped-def]
        payload = store.resolve(store.ref_for_provisioning(attempt_id))
        observed.append(
            base64.b64decode(payload[STATE_FIELD]) if payload is not None else None
        )
        return original_update(attempt_id, **fields)  # type: ignore[arg-type]

    monkeypatch.setattr(repository, "update_provisioning", _observe)

    result = await flow.poll(view.attempt_id)

    assert result.attempt.qr_content == "qr-2"
    # 关键顺序：二维码落库时，状态文件里**已经**是新会话（反序会让新二维码扫不出结果）
    assert observed[0] == rotated
    stored = repository.get_provisioning(view.attempt_id)
    assert stored is not None
    # 引用不变（同一个接入尝试），内容已换成新会话
    assert stored.state_ref == store.ref_for_provisioning(view.attempt_id)
    payload = store.resolve(stored.state_ref)
    assert payload is not None
    assert base64.b64decode(payload[STATE_FIELD]) == rotated
    await flow.poll(view.attempt_id, force=True)
    assert driver.poll_calls[-1] == rotated


@pytest.mark.asyncio
async def test_failed_state_rotation_fails_the_attempt(
    repository: ChannelRepository, store: ChannelCredentialStore, monkeypatch
) -> None:
    """状态写不进去（磁盘满、权限被改）时只能让这次接入失败：留着旧会话继续轮询毫无意义。"""
    driver = FakeDriver(
        outcomes=[
            ProvisioningOutcome(status=STATUS_WAITING, qr_content="qr-2", state=b"NEW")
        ]
    )
    flow = _flow(repository, store, driver)
    view = await flow.begin(channel="wecom_bot", owner_ref="o")

    def _boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("disk full")

    monkeypatch.setattr(store, "write", _boom)

    result = await flow.poll(view.attempt_id)

    assert result.attempt.status == STATUS_FAILED
    assert result.attempt.error_code == err.CHANNEL_PROVISIONING_FAILED


# ==============================================================================
# 9.1 第 3 条：接入过期
# ==============================================================================


@pytest.mark.asyncio
async def test_expired_clears_the_stored_state(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    driver = FakeDriver(
        session=ProvisioningSession(
            qr_content="qr",
            expires_at=_now() - timedelta(seconds=1),
            poll_interval_ms=0,
            state=INSTANCE_LESS_STATE,
        )
    )
    flow = _flow(repository, store, driver)
    view = await flow.begin(channel="wecom_bot", owner_ref="o")

    result = await flow.poll(view.attempt_id)

    assert result.attempt.status == STATUS_EXPIRED
    stored = repository.get_provisioning(view.attempt_id)
    assert stored is not None
    assert stored.state_ref is None
    assert store.resolve(store.ref_for_provisioning(view.attempt_id)) is None
    # 平台都没有被问过：过期判定优先于轮询
    assert driver.poll_calls == []


@pytest.mark.asyncio
async def test_driver_reported_expiry_clears_state(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    driver = FakeDriver(outcomes=[ProvisioningOutcome(status=STATUS_EXPIRED)])
    flow = _flow(repository, store, driver)
    view = await flow.begin(channel="wecom_bot", owner_ref="o")

    result = await flow.poll(view.attempt_id)

    assert result.attempt.status == STATUS_EXPIRED
    stored = repository.get_provisioning(view.attempt_id)
    assert stored is not None
    assert stored.state_ref is None
    assert store.resolve(store.ref_for_provisioning(view.attempt_id)) is None


# ==============================================================================
# 9.1 第 4 条：取消与手填
# ==============================================================================


@pytest.mark.asyncio
async def test_cancel_stops_polling(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    driver = FakeDriver()
    flow = _flow(repository, store, driver)
    view = await flow.begin(channel="wecom_bot", owner_ref="o")

    cancelled = await flow.cancel(view.attempt_id)

    assert cancelled.status == "cancelled"
    stored = repository.get_provisioning(view.attempt_id)
    assert stored is not None
    assert stored.state_ref is None
    assert store.resolve(store.ref_for_provisioning(view.attempt_id)) is None

    # 取消之后即使再被轮询也不会去问平台
    result = await flow.poll(view.attempt_id)
    assert result.attempt.status == "cancelled"
    assert driver.poll_calls == []


@pytest.mark.asyncio
async def test_manual_path_shares_persistence_order(
    repository: ChannelRepository,
    store: ChannelCredentialStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手填凭据走与扫码完全相同的落库顺序：先凭据、后配置。"""
    order: list[tuple[str, dict]] = []
    original_create = repository.create_instance
    original_update = repository.update_instance

    def _create(**kwargs):  # type: ignore[no-untyped-def]
        order.append(("create", dict(kwargs)))
        return original_create(**kwargs)

    def _update(instance_id, **kwargs):  # type: ignore[no-untyped-def]
        order.append(("update", dict(kwargs)))
        return original_update(instance_id, **kwargs)

    monkeypatch.setattr(repository, "create_instance", _create)
    monkeypatch.setattr(repository, "update_instance", _update)

    binder = ManualCredentialBinder(repository=repository, store=store)
    instance = await binder.bind(
        channel="wecom_bot",
        credentials={"bot_id": "bot-12345678", "secret": "s3cr3t"},
        owner_ref="owner-1",
        agent_id="agent-1",
    )

    assert [step for step, _ in order] == ["create", "update"]
    created = order[0][1]
    assert created["credential_ref"] is not None
    assert created["credential_mask"] == "bot-****5678"
    assert created["config"] == {}
    assert order[1][1]["config"] == {"bot_id": "bot-12345678"}
    assert instance.credential_mask == "bot-****5678"


@pytest.mark.asyncio
async def test_manual_bind_requires_credentials(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    binder = ManualCredentialBinder(repository=repository, store=store)

    with pytest.raises(DomainError) as excinfo:
        await binder.bind(channel="wecom_bot", credentials={})

    assert excinfo.value.code == err.CHANNEL_CREDENTIALS_INVALID


@pytest.mark.asyncio
async def test_manual_bind_rejects_unknown_channel(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    binder = ManualCredentialBinder(repository=repository, store=store)

    with pytest.raises(DomainError) as excinfo:
        await binder.bind(channel="nope", credentials={"secret": "s"})

    assert excinfo.value.code == err.CHANNEL_ADAPTER_UNKNOWN


@pytest.mark.asyncio
async def test_rollback_leaves_no_half_state(
    repository: ChannelRepository,
    store: ChannelCredentialStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任一步失败即回滚：不出现"有配置没凭据"的半成品，也不留下实例行。"""
    original_update = repository.update_instance

    def _failing_update(instance_id, **kwargs):  # type: ignore[no-untyped-def]
        if "config" in kwargs:
            raise RuntimeError("config write failed")
        return original_update(instance_id, **kwargs)

    monkeypatch.setattr(repository, "update_instance", _failing_update)
    driver = FakeDriver(
        outcomes=[
            ProvisioningOutcome(
                status=STATUS_SUCCEEDED,
                credentials={"bot_id": "bot-12345678", "secret": "s3cr3t"},
            )
        ]
    )
    flow = _flow(repository, store, driver)
    view = await flow.begin(channel="wecom_bot", owner_ref="o")

    result = await flow.poll(view.attempt_id)

    assert result.attempt.status == STATUS_FAILED
    assert result.instance is None
    assert repository.list_instances() == []
    stored = repository.get_provisioning(view.attempt_id)
    assert stored is not None
    assert stored.state_ref is None
    assert store.resolve(store.ref_for_provisioning(view.attempt_id)) is None


@pytest.mark.asyncio
async def test_missing_required_credential_field_fails_without_instance(
    repository: ChannelRepository, store: ChannelCredentialStore
) -> None:
    driver = FakeDriver(
        outcomes=[
            ProvisioningOutcome(status=STATUS_SUCCEEDED, credentials={"bot_id": "b"})
        ]
    )
    flow = _flow(repository, store, driver)
    view = await flow.begin(channel="wecom_bot", owner_ref="o")

    result = await flow.poll(view.attempt_id)

    assert result.attempt.status == STATUS_FAILED
    assert repository.list_instances() == []


# ==============================================================================
# 驱动注册表
# ==============================================================================


def test_registered_drivers_cover_the_shipped_channels() -> None:
    """随包发布的两个渠道都能扫码接入：/channels/catalog 的 supports_provisioning 取的就是这张表。"""
    import witty_service.channels.adapters  # noqa: F401 - 触发适配器与驱动自注册

    drivers = set(driver_module.registered_driver_channels())

    assert {"qq_bot", "wecom_bot"} <= drivers
    assert drivers <= set(registered_channels())


def test_register_driver_rejects_duplicate() -> None:
    class _Other:
        channel = "duplicate_probe"

        async def begin(self):  # type: ignore[no-untyped-def]
            raise NotImplementedError

        async def poll(self, state: bytes):  # type: ignore[no-untyped-def]
            raise NotImplementedError

    driver_module.register_driver(_Other)  # type: ignore[arg-type]

    class _Conflict:
        channel = "duplicate_probe"

    with pytest.raises(ValueError):
        driver_module.register_driver(_Conflict)  # type: ignore[arg-type]
    driver_module.DRIVER_REGISTRY.pop("duplicate_probe", None)
