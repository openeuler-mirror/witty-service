"""W7 渠道仓储的测试：真实 SQLite 临时库（不是 mock）。

覆盖两条实施计划点名的用例：
- **唯一约束冲突被识别为重复事件**（去重的原子性建立在数据库约束上）；
- **级联删除按预期生效**（删实例 → 路由与准入策略一起消失）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from witty_service.persistence.channel_repository import (
    UNSET,
    ChannelRepository,
    retention_cutoff,
)
from witty_service.persistence.db import create_session_factory, create_sqlite_engine
from witty_service.persistence.orm import ProvisioningStatus
from witty_service.persistence.repositories import SqliteRepository


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def repository(tmp_path) -> ChannelRepository:
    # 用 create_sqlite_engine 才能在测试里真正验证外键级联（PRAGMA foreign_keys=ON）
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'channel.sqlite3'}")
    from witty_service.persistence.orm import Base

    Base.metadata.create_all(engine)
    yield ChannelRepository(create_session_factory(engine))
    engine.dispose()


# ==============================================================================
# 渠道实例
# ==============================================================================


def test_create_and_read_instance(repository: ChannelRepository) -> None:
    created = repository.create_instance(
        channel="wecom_bot",
        display_name="运维助手",
        owner_ref="owner-1",
        agent_id="agent-1",
        config={"bot_id": "bot-1234"},
        credential_ref="chan_" + "a" * 32,
        credential_mask="bot-****1234",
    )

    loaded = repository.get_instance(created.id)

    assert loaded is not None
    assert loaded.channel == "wecom_bot"
    assert loaded.status == "pending"
    assert loaded.generation == 1
    assert loaded.config == {"bot_id": "bot-1234"}
    assert loaded.credential_ref == "chan_" + "a" * 32
    assert loaded.credential_mask == "bot-****1234"


def test_update_instance_distinguishes_unset_from_null(
    repository: ChannelRepository,
) -> None:
    instance = repository.create_instance(channel="qq_bot", agent_id="agent-1")

    untouched = repository.update_instance(instance.id, display_name="新名字")
    assert untouched is not None
    assert untouched.agent_id == "agent-1"

    cleared = repository.update_instance(instance.id, agent_id=None)
    assert cleared is not None
    assert cleared.agent_id is None
    assert cleared.display_name == "新名字"


def test_list_instances_filters(repository: ChannelRepository) -> None:
    repository.create_instance(channel="wecom_bot", owner_ref="o1")
    repository.create_instance(channel="qq_bot", owner_ref="o1")
    repository.create_instance(channel="qq_bot", owner_ref="o2", status="connected")

    assert len(repository.list_instances()) == 3
    assert len(repository.list_instances(owner_ref="o1")) == 2
    assert len(repository.list_instances(channel="qq_bot")) == 2
    assert len(repository.list_instances(status="connected")) == 1


def test_bump_generation_on_rebuild(repository: ChannelRepository) -> None:
    instance = repository.create_instance(channel="wecom_bot")

    updated = repository.update_instance(instance.id, bump_generation=True)

    assert updated is not None
    assert updated.generation == 2


def test_delete_instance_cascades_routes_and_policies(
    repository: ChannelRepository,
) -> None:
    """级联删除按预期生效：路由与准入策略随实例一起消失。"""
    instance = repository.create_instance(channel="wecom_bot")
    route = repository.get_or_create_route(
        instance_id=instance.id, conversation_type="direct", platform_user_id="u1"
    )
    repository.upsert_access_policy(
        instance_id=instance.id, conversation_type="direct", mode="allowlist",
        allowlist=["u1"],
    )

    assert repository.delete_instance(instance.id) is True

    assert repository.get_route(route.id) is None
    assert repository.list_routes_for_instance(instance.id) == []
    assert repository.list_access_policies(instance.id) == []
    assert repository.delete_instance(instance.id) is False


# ==============================================================================
# 会话路由
# ==============================================================================


def test_get_or_create_route_is_idempotent(repository: ChannelRepository) -> None:
    instance = repository.create_instance(channel="wecom_bot")

    first = repository.get_or_create_route(
        instance_id=instance.id, conversation_type="direct", platform_user_id="u1"
    )
    second = repository.get_or_create_route(
        instance_id=instance.id, conversation_type="direct", platform_user_id="u1"
    )

    assert first.id == second.id


def test_routes_are_isolated_by_user_and_conversation_type(
    repository: ChannelRepository,
) -> None:
    instance = repository.create_instance(channel="wecom_bot")

    direct = repository.get_or_create_route(
        instance_id=instance.id, conversation_type="direct", platform_user_id="u1"
    )
    other_user = repository.get_or_create_route(
        instance_id=instance.id, conversation_type="direct", platform_user_id="u2"
    )
    group = repository.get_or_create_route(
        instance_id=instance.id, conversation_type="group", platform_user_id="u1"
    )

    assert len({direct.id, other_user.id, group.id}) == 3


def test_placeholder_ref_round_trip(repository: ChannelRepository) -> None:
    instance = repository.create_instance(channel="wecom_bot")
    route = repository.get_or_create_route(
        instance_id=instance.id, conversation_type="direct", platform_user_id="u1"
    )

    repository.set_active_session(route.id, "session-1")
    repository.set_active_placeholder_ref(route.id, "msg-1")
    loaded = repository.get_route(route.id)

    assert loaded is not None
    assert loaded.active_session_id == "session-1"
    assert loaded.active_placeholder_ref == "msg-1"

    repository.set_active_placeholder_ref(route.id, None)
    cleared = repository.get_route(route.id)
    assert cleared is not None
    assert cleared.active_placeholder_ref is None


# ==============================================================================
# 入站去重
# ==============================================================================


def test_duplicate_event_is_recognized(repository: ChannelRepository) -> None:
    """唯一约束冲突被识别为重复事件（同一事件登记两次，第二次返回 False）。"""
    instance = repository.create_instance(channel="wecom_bot")

    assert (
        repository.register_inbound_event(
            instance_id=instance.id, platform_event_id="evt-1"
        )
        is True
    )
    assert (
        repository.register_inbound_event(
            instance_id=instance.id, platform_event_id="evt-1"
        )
        is False
    )
    assert repository.count_inbound_events(instance_id=instance.id) == 1


def test_same_event_id_on_other_instance_is_not_duplicate(
    repository: ChannelRepository,
) -> None:
    first = repository.create_instance(channel="wecom_bot")
    second = repository.create_instance(channel="wecom_bot")

    assert repository.register_inbound_event(
        instance_id=first.id, platform_event_id="evt-1"
    )
    assert repository.register_inbound_event(
        instance_id=second.id, platform_event_id="evt-1"
    )


def test_prune_inbound_events(repository: ChannelRepository) -> None:
    instance = repository.create_instance(channel="wecom_bot")
    old = _now() - timedelta(days=30)
    fresh = _now()
    repository.register_inbound_event(
        instance_id=instance.id, platform_event_id="old", received_at=old
    )
    repository.register_inbound_event(
        instance_id=instance.id, platform_event_id="fresh", received_at=fresh
    )

    deleted = repository.prune_inbound_events(before=retention_cutoff(days=7))

    assert deleted == 1
    assert repository.count_inbound_events(instance_id=instance.id) == 1


# ==============================================================================
# 接入尝试
# ==============================================================================


def test_provisioning_lifecycle(repository: ChannelRepository) -> None:
    attempt = repository.create_provisioning(
        channel="wecom_bot",
        owner_ref="o1",
        agent_id="agent-1",
        poll_interval_ms=2000,
        expires_at=_now() + timedelta(minutes=5),
        qr_content="https://example/qr",
        state_ref="prov_" + "b" * 32,
    )

    waiting = repository.find_waiting_provisioning(channel="wecom_bot", owner_ref="o1")
    assert waiting is not None
    assert waiting.id == attempt.id

    done = repository.update_provisioning(
        attempt.id,
        status=ProvisioningStatus.succeeded.value,
        state_ref=None,
        error_code=None,
    )
    assert done is not None
    assert done.state_ref is None
    # 成功之后不再属于"进行中的尝试"
    assert (
        repository.find_waiting_provisioning(channel="wecom_bot", owner_ref="o1")
        is None
    )


def test_find_waiting_provisioning_ignores_expired(
    repository: ChannelRepository,
) -> None:
    repository.create_provisioning(
        channel="wecom_bot",
        owner_ref="o1",
        poll_interval_ms=2000,
        expires_at=_now() - timedelta(seconds=1),
    )

    assert (
        repository.find_waiting_provisioning(channel="wecom_bot", owner_ref="o1")
        is None
    )


def test_prune_provisionings_removes_expired_and_cancelled(
    repository: ChannelRepository,
) -> None:
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
    repository.create_provisioning(
        channel="wecom_bot",
        owner_ref="waiting",
        poll_interval_ms=1000,
        expires_at=_now() + timedelta(minutes=5),
    )

    assert repository.prune_provisionings() == 2


def test_update_provisioning_clears_state_on_expiry(
    repository: ChannelRepository,
) -> None:
    attempt = repository.create_provisioning(
        channel="wecom_bot",
        owner_ref="o1",
        poll_interval_ms=1000,
        expires_at=_now() + timedelta(minutes=5),
        state_ref="prov_" + "b" * 32,
    )

    expired = repository.update_provisioning(
        attempt.id,
        status=ProvisioningStatus.expired.value,
        state_ref=None,
    )

    assert expired is not None
    assert expired.status == "expired"
    assert expired.state_ref is None


# ==============================================================================
# 投递记录与准入策略
# ==============================================================================


def test_record_and_list_deliveries(repository: ChannelRepository) -> None:
    instance = repository.create_instance(channel="wecom_bot")
    route = repository.get_or_create_route(
        instance_id=instance.id, conversation_type="direct", platform_user_id="u1"
    )

    repository.record_delivery(
        instance_id=instance.id,
        route_id=route.id,
        session_id="session-1",
        turn_id="turn-1",
        certainty="delivered",
        segment_index=0,
        platform_message_ref="msg-1",
    )
    repository.record_delivery(
        instance_id=instance.id,
        route_id=route.id,
        turn_id="turn-1",
        certainty="uncertain",
        segment_index=1,
        error_code="CHANNEL_DELIVERY_UNCERTAIN",
    )

    records = repository.list_deliveries(instance_id=instance.id)
    assert len(records) == 2
    assert {record.certainty for record in records} == {"delivered", "uncertain"}
    assert len(repository.list_deliveries(turn_id="turn-1")) == 2
    assert len(repository.list_deliveries(turn_id="turn-2")) == 0


def test_upsert_access_policy_overwrites(repository: ChannelRepository) -> None:
    instance = repository.create_instance(channel="wecom_bot")

    created = repository.upsert_access_policy(
        instance_id=instance.id, conversation_type="direct", mode="open"
    )
    updated = repository.upsert_access_policy(
        instance_id=instance.id,
        conversation_type="direct",
        mode="allowlist",
        allowlist=["u1", "u2"],
    )

    assert created.id == updated.id
    assert updated.mode == "allowlist"
    assert updated.allowlist == ["u1", "u2"]
    assert repository.get_access_policy(
        instance_id=instance.id, conversation_type="group"
    ) is None


def test_access_policy_unique_constraint(repository: ChannelRepository) -> None:
    """同一 (实例, 聊天类型) 只能有一条策略。"""
    from witty_service.persistence.orm import ChannelAccessPolicyORM

    instance = repository.create_instance(channel="wecom_bot")
    repository.upsert_access_policy(
        instance_id=instance.id, conversation_type="direct", mode="open"
    )

    with repository._session_factory() as session:
        session.add(
            ChannelAccessPolicyORM(
                id="dup",
                channel_instance_id=instance.id,
                conversation_type="direct",
                mode="open",
                allowlist=[],
                allow_commands=True,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_sqlite_repository_shares_session_factory(tmp_path) -> None:
    """渠道仓储与 SqliteRepository 共用同一引擎，不新建连接。"""
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'shared.sqlite3'}")
    try:
        factory = create_session_factory(engine)
        repository = SqliteRepository(factory)

        assert repository.session_factory is factory
        assert isinstance(ChannelRepository(repository.session_factory), ChannelRepository)
    finally:
        engine.dispose()


def test_unset_sentinel_is_not_none() -> None:
    assert UNSET is not None
