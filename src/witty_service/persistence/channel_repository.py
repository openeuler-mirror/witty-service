"""渠道仓储：`channel_*` 六张表的读写。

实施计划的细化（§6 W7）：**不把约 25 个方法塞进已经 2400 行的 `SqliteRepository`**，
而是独立模块；构造参数与 `SqliteRepository` 一样是 `session_factory`，两者共用
同一个引擎（`SqliteRepository.session_factory`），**不新建数据库连接**。

本模块只做持久化，不含任何业务编排：三态归类、准入判定、自愈重建会话等全部
在 `channels/` 内完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from witty_service.persistence.orm import (
    ChannelAccessPolicyORM,
    ChannelDeliveryORM,
    ChannelInboundEventORM,
    ChannelInstanceORM,
    ChannelInstanceStatus,
    ChannelProvisioningORM,
    ChannelRouteORM,
    ProvisioningStatus,
)


class _Unset:
    """显式区分"未提供该字段"与"把该字段置空"（`None` 表示置空）。"""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


#: `update_*` 方法的哨兵值
UNSET = _Unset()

_OptionalStr = "str | None | _Unset"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class ChannelInstanceRecord:
    id: str
    channel: str
    display_name: str | None
    owner_ref: str | None
    agent_id: str | None
    status: str
    generation: int
    config: dict[str, Any]
    #: 凭据文件的引用（非密）；凭据本体不在主库里
    credential_ref: str | None
    credential_mask: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class ChannelProvisioningRecord:
    id: str
    channel: str
    owner_ref: str | None
    agent_id: str | None
    status: str
    qr_content: str | None
    poll_interval_ms: int
    expires_at: datetime
    #: 平台临时凭据文件的引用（非密）；接入结束即置空并删除文件
    state_ref: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class ChannelRouteRecord:
    id: str
    channel_instance_id: str
    conversation_type: str
    platform_user_id: str
    active_session_id: str | None
    active_placeholder_ref: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class ChannelDeliveryRecord:
    id: str
    channel_instance_id: str
    route_id: str | None
    session_id: str | None
    turn_id: str | None
    certainty: str
    segment_index: int
    platform_message_ref: str | None
    error_code: str | None
    created_at: datetime


@dataclass(slots=True)
class ChannelAccessPolicyRecord:
    id: str
    channel_instance_id: str
    conversation_type: str
    mode: str
    allowlist: list[str]
    allow_commands: bool
    updated_at: datetime


#: 接入尝试的终态：这些状态不需要等待过期即可被周期清理回收
PROVISIONING_TERMINAL_STATUSES = (
    ProvisioningStatus.cancelled.value,
    ProvisioningStatus.failed.value,
)


class ChannelRepository:
    """`channel_*` 六张表的读写。所有方法同步、无 IO 编排、每个方法一个事务。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # ==========================================================================
    # 渠道实例
    # ==========================================================================

    def create_instance(
        self,
        *,
        channel: str,
        display_name: str | None = None,
        owner_ref: str | None = None,
        agent_id: str | None = None,
        status: str = ChannelInstanceStatus.pending.value,
        config: dict[str, Any] | None = None,
        credential_ref: str | None = None,
        credential_mask: str | None = None,
        instance_id: str | None = None,
    ) -> ChannelInstanceRecord:
        with self._session_factory() as session:
            row = ChannelInstanceORM(
                id=instance_id or str(uuid4()),
                channel=channel,
                display_name=display_name,
                owner_ref=owner_ref,
                agent_id=agent_id,
                status=status,
                generation=1,
                config=dict(config or {}),
                credential_ref=credential_ref,
                credential_mask=credential_mask,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_instance_record(row)

    def get_instance(self, instance_id: str) -> ChannelInstanceRecord | None:
        with self._session_factory() as session:
            row = session.get(ChannelInstanceORM, instance_id)
            return None if row is None else self._to_instance_record(row)

    def list_instances(
        self,
        *,
        owner_ref: str | None = None,
        channel: str | None = None,
        status: str | None = None,
    ) -> list[ChannelInstanceRecord]:
        with self._session_factory() as session:
            query = session.query(ChannelInstanceORM)
            if owner_ref is not None:
                query = query.filter(ChannelInstanceORM.owner_ref == owner_ref)
            if channel is not None:
                query = query.filter(ChannelInstanceORM.channel == channel)
            if status is not None:
                query = query.filter(ChannelInstanceORM.status == status)
            rows = query.order_by(ChannelInstanceORM.created_at.asc()).all()
            return [self._to_instance_record(row) for row in rows]

    def list_instances_for_agent(self, agent_id: str) -> list[ChannelInstanceRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(ChannelInstanceORM)
                .filter(ChannelInstanceORM.agent_id == agent_id)
                .order_by(ChannelInstanceORM.created_at.asc())
                .all()
            )
            return [self._to_instance_record(row) for row in rows]

    def update_instance(
        self,
        instance_id: str,
        *,
        display_name: str | _Unset | None = UNSET,
        agent_id: str | _Unset | None = UNSET,
        status: str | _Unset = UNSET,
        config: dict[str, Any] | _Unset = UNSET,
        credential_ref: str | _Unset | None = UNSET,
        credential_mask: str | _Unset | None = UNSET,
        bump_generation: bool = False,
    ) -> ChannelInstanceRecord | None:
        with self._session_factory() as session:
            row = session.get(ChannelInstanceORM, instance_id)
            if row is None:
                return None
            if not isinstance(display_name, _Unset):
                row.display_name = display_name
            if not isinstance(agent_id, _Unset):
                row.agent_id = agent_id
            if not isinstance(status, _Unset):
                row.status = status
            if not isinstance(config, _Unset):
                row.config = dict(config)
            if not isinstance(credential_ref, _Unset):
                row.credential_ref = credential_ref
            if not isinstance(credential_mask, _Unset):
                row.credential_mask = credential_mask
            if bump_generation:
                row.generation = int(row.generation) + 1
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return self._to_instance_record(row)

    def delete_instance(self, instance_id: str) -> bool:
        """删除实例；路由与准入策略按外键级联删除（框架设计 §5.1）。"""
        with self._session_factory() as session:
            row = session.get(ChannelInstanceORM, instance_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    # ==========================================================================
    # 接入尝试
    # ==========================================================================

    def create_provisioning(
        self,
        *,
        channel: str,
        expires_at: datetime,
        poll_interval_ms: int,
        owner_ref: str | None = None,
        agent_id: str | None = None,
        status: str = ProvisioningStatus.waiting.value,
        qr_content: str | None = None,
        state_ref: str | None = None,
        attempt_id: str | None = None,
    ) -> ChannelProvisioningRecord:
        with self._session_factory() as session:
            row = ChannelProvisioningORM(
                id=attempt_id or str(uuid4()),
                channel=channel,
                owner_ref=owner_ref,
                agent_id=agent_id,
                status=status,
                qr_content=qr_content,
                poll_interval_ms=poll_interval_ms,
                expires_at=expires_at,
                state_ref=state_ref,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_provisioning_record(row)

    def get_provisioning(self, attempt_id: str) -> ChannelProvisioningRecord | None:
        with self._session_factory() as session:
            row = session.get(ChannelProvisioningORM, attempt_id)
            return None if row is None else self._to_provisioning_record(row)

    def find_waiting_provisioning(
        self,
        *,
        channel: str,
        owner_ref: str | None,
        now: datetime | None = None,
    ) -> ChannelProvisioningRecord | None:
        """查找同一 (渠道, 归属标签) 尚未结束的接入尝试。

        接入尝试发生在渠道实例**建立之前**，因此库中没有 `channel_instance_id`；
        "同一个渠道实例同时只允许一个进行中的接入尝试"在实现上等价于
        "同一 (channel, owner_ref) 只有一条 waiting 且未过期的尝试"。
        """
        moment = now or utcnow()
        with self._session_factory() as session:
            query = session.query(ChannelProvisioningORM).filter(
                ChannelProvisioningORM.channel == channel,
                ChannelProvisioningORM.status == ProvisioningStatus.waiting.value,
                ChannelProvisioningORM.expires_at > moment,
            )
            if owner_ref is None:
                query = query.filter(ChannelProvisioningORM.owner_ref.is_(None))
            else:
                query = query.filter(ChannelProvisioningORM.owner_ref == owner_ref)
            row = query.order_by(ChannelProvisioningORM.created_at.desc()).first()
            return None if row is None else self._to_provisioning_record(row)

    def update_provisioning(
        self,
        attempt_id: str,
        *,
        status: str | _Unset = UNSET,
        qr_content: str | _Unset | None = UNSET,
        state_ref: str | _Unset | None = UNSET,
        error_code: str | _Unset | None = UNSET,
        agent_id: str | _Unset | None = UNSET,
        expires_at: datetime | _Unset = UNSET,
    ) -> ChannelProvisioningRecord | None:
        with self._session_factory() as session:
            row = session.get(ChannelProvisioningORM, attempt_id)
            if row is None:
                return None
            if not isinstance(status, _Unset):
                row.status = status
            if not isinstance(qr_content, _Unset):
                row.qr_content = qr_content
            if not isinstance(state_ref, _Unset):
                row.state_ref = state_ref
            if not isinstance(error_code, _Unset):
                row.error_code = error_code
            if not isinstance(agent_id, _Unset):
                row.agent_id = agent_id
            if not isinstance(expires_at, _Unset):
                row.expires_at = expires_at
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return self._to_provisioning_record(row)

    def prune_provisionings(self, *, now: datetime | None = None) -> int:
        """回收已过期、已取消或已失败的接入尝试（增长有界，框架设计 §3.9）。"""
        moment = now or utcnow()
        with self._session_factory() as session:
            deleted = (
                session.query(ChannelProvisioningORM)
                .filter(
                    (ChannelProvisioningORM.expires_at < moment)
                    | (
                        ChannelProvisioningORM.status.in_(
                            list(PROVISIONING_TERMINAL_STATUSES)
                        )
                    )
                )
                .delete(synchronize_session=False)
            )
            session.commit()
            return int(deleted or 0)

    # ==========================================================================
    # 会话路由
    # ==========================================================================

    def get_or_create_route(
        self,
        *,
        instance_id: str,
        conversation_type: str,
        platform_user_id: str,
    ) -> ChannelRouteRecord:
        with self._session_factory() as session:
            row = self._find_route_row(
                session, instance_id, conversation_type, platform_user_id
            )
            if row is None:
                row = ChannelRouteORM(
                    id=str(uuid4()),
                    channel_instance_id=instance_id,
                    conversation_type=conversation_type,
                    platform_user_id=platform_user_id,
                )
                session.add(row)
                try:
                    session.commit()
                except IntegrityError:
                    # 并发首次入站：唯一约束保证只有一条路由，退化为读取既有行
                    session.rollback()
                    row = self._find_route_row(
                        session, instance_id, conversation_type, platform_user_id
                    )
                    if row is None:  # pragma: no cover - 约束冲突但读不到，属异常
                        raise
                else:
                    session.refresh(row)
            return self._to_route_record(row)

    def get_route(self, route_id: str) -> ChannelRouteRecord | None:
        with self._session_factory() as session:
            row = session.get(ChannelRouteORM, route_id)
            return None if row is None else self._to_route_record(row)

    def list_routes_for_instance(self, instance_id: str) -> list[ChannelRouteRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(ChannelRouteORM)
                .filter(ChannelRouteORM.channel_instance_id == instance_id)
                .order_by(ChannelRouteORM.created_at.asc())
                .all()
            )
            return [self._to_route_record(row) for row in rows]

    def set_active_session(
        self, route_id: str, session_id: str | None
    ) -> ChannelRouteRecord | None:
        with self._session_factory() as session:
            row = session.get(ChannelRouteORM, route_id)
            if row is None:
                return None
            row.active_session_id = session_id
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return self._to_route_record(row)

    def set_active_placeholder_ref(
        self, route_id: str, placeholder_ref: str | None
    ) -> ChannelRouteRecord | None:
        with self._session_factory() as session:
            row = session.get(ChannelRouteORM, route_id)
            if row is None:
                return None
            row.active_placeholder_ref = placeholder_ref
            row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return self._to_route_record(row)

    # ==========================================================================
    # 入站去重
    # ==========================================================================

    def register_inbound_event(
        self,
        *,
        instance_id: str,
        platform_event_id: str,
        received_at: datetime | None = None,
    ) -> bool:
        """登记一次入站事件；返回 False 表示该事件已登记过（重复投递，丢弃）。"""
        with self._session_factory() as session:
            session.add(
                ChannelInboundEventORM(
                    id=str(uuid4()),
                    channel_instance_id=instance_id,
                    platform_event_id=platform_event_id,
                    received_at=received_at or utcnow(),
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                return False
            return True

    def prune_inbound_events(self, *, before: datetime) -> int:
        with self._session_factory() as session:
            deleted = (
                session.query(ChannelInboundEventORM)
                .filter(ChannelInboundEventORM.received_at < before)
                .delete(synchronize_session=False)
            )
            session.commit()
            return int(deleted or 0)

    def count_inbound_events(self, *, instance_id: str | None = None) -> int:
        with self._session_factory() as session:
            query = session.query(ChannelInboundEventORM)
            if instance_id is not None:
                query = query.filter(
                    ChannelInboundEventORM.channel_instance_id == instance_id
                )
            return int(query.count())

    # ==========================================================================
    # 出站投递记录
    # ==========================================================================

    def record_delivery(
        self,
        *,
        instance_id: str,
        certainty: str,
        segment_index: int,
        route_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        platform_message_ref: str | None = None,
        error_code: str | None = None,
    ) -> ChannelDeliveryRecord:
        with self._session_factory() as session:
            row = ChannelDeliveryORM(
                id=str(uuid4()),
                channel_instance_id=instance_id,
                route_id=route_id,
                session_id=session_id,
                turn_id=turn_id,
                certainty=certainty,
                segment_index=segment_index,
                platform_message_ref=platform_message_ref,
                error_code=error_code,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_delivery_record(row)

    def list_deliveries(
        self,
        *,
        instance_id: str | None = None,
        route_id: str | None = None,
        turn_id: str | None = None,
        limit: int = 100,
    ) -> list[ChannelDeliveryRecord]:
        with self._session_factory() as session:
            query = session.query(ChannelDeliveryORM)
            if instance_id is not None:
                query = query.filter(ChannelDeliveryORM.channel_instance_id == instance_id)
            if route_id is not None:
                query = query.filter(ChannelDeliveryORM.route_id == route_id)
            if turn_id is not None:
                query = query.filter(ChannelDeliveryORM.turn_id == turn_id)
            rows = (
                query.order_by(ChannelDeliveryORM.created_at.desc())
                .limit(max(1, limit))
                .all()
            )
            return [self._to_delivery_record(row) for row in rows]

    # ==========================================================================
    # 准入策略
    # ==========================================================================

    def get_access_policy(
        self, *, instance_id: str, conversation_type: str
    ) -> ChannelAccessPolicyRecord | None:
        with self._session_factory() as session:
            row = (
                session.query(ChannelAccessPolicyORM)
                .filter(
                    ChannelAccessPolicyORM.channel_instance_id == instance_id,
                    ChannelAccessPolicyORM.conversation_type == conversation_type,
                )
                .first()
            )
            return None if row is None else self._to_access_policy_record(row)

    def list_access_policies(self, instance_id: str) -> list[ChannelAccessPolicyRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(ChannelAccessPolicyORM)
                .filter(ChannelAccessPolicyORM.channel_instance_id == instance_id)
                .order_by(ChannelAccessPolicyORM.conversation_type.asc())
                .all()
            )
            return [self._to_access_policy_record(row) for row in rows]

    def upsert_access_policy(
        self,
        *,
        instance_id: str,
        conversation_type: str,
        mode: str,
        allowlist: list[str] | None = None,
        allow_commands: bool = True,
    ) -> ChannelAccessPolicyRecord:
        """写入准入策略；**写入后立即生效**（每次判定都从库读取，不做进程内快照）。"""
        entries = list(allowlist or [])
        with self._session_factory() as session:
            row = (
                session.query(ChannelAccessPolicyORM)
                .filter(
                    ChannelAccessPolicyORM.channel_instance_id == instance_id,
                    ChannelAccessPolicyORM.conversation_type == conversation_type,
                )
                .first()
            )
            if row is None:
                row = ChannelAccessPolicyORM(
                    id=str(uuid4()),
                    channel_instance_id=instance_id,
                    conversation_type=conversation_type,
                    mode=mode,
                    allowlist=entries,
                    allow_commands=allow_commands,
                )
                session.add(row)
            else:
                row.mode = mode
                row.allowlist = entries
                row.allow_commands = allow_commands
                row.updated_at = utcnow()
            session.commit()
            session.refresh(row)
            return self._to_access_policy_record(row)

    # ==========================================================================
    # 内部工具
    # ==========================================================================

    @staticmethod
    def _find_route_row(
        session: Session,
        instance_id: str,
        conversation_type: str,
        platform_user_id: str,
    ) -> ChannelRouteORM | None:
        return (
            session.query(ChannelRouteORM)
            .filter(
                ChannelRouteORM.channel_instance_id == instance_id,
                ChannelRouteORM.conversation_type == conversation_type,
                ChannelRouteORM.platform_user_id == platform_user_id,
            )
            .first()
        )

    @staticmethod
    def _to_instance_record(row: ChannelInstanceORM) -> ChannelInstanceRecord:
        return ChannelInstanceRecord(
            id=row.id,
            channel=row.channel,
            display_name=row.display_name,
            owner_ref=row.owner_ref,
            agent_id=row.agent_id,
            status=row.status,
            generation=int(row.generation),
            config=dict(row.config or {}),
            credential_ref=row.credential_ref,
            credential_mask=row.credential_mask,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_provisioning_record(row: ChannelProvisioningORM) -> ChannelProvisioningRecord:
        return ChannelProvisioningRecord(
            id=row.id,
            channel=row.channel,
            owner_ref=row.owner_ref,
            agent_id=row.agent_id,
            status=row.status,
            qr_content=row.qr_content,
            poll_interval_ms=int(row.poll_interval_ms),
            expires_at=row.expires_at,
            state_ref=row.state_ref,
            error_code=row.error_code,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_route_record(row: ChannelRouteORM) -> ChannelRouteRecord:
        return ChannelRouteRecord(
            id=row.id,
            channel_instance_id=row.channel_instance_id,
            conversation_type=row.conversation_type,
            platform_user_id=row.platform_user_id,
            active_session_id=row.active_session_id,
            active_placeholder_ref=row.active_placeholder_ref,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_delivery_record(row: ChannelDeliveryORM) -> ChannelDeliveryRecord:
        return ChannelDeliveryRecord(
            id=row.id,
            channel_instance_id=row.channel_instance_id,
            route_id=row.route_id,
            session_id=row.session_id,
            turn_id=row.turn_id,
            certainty=row.certainty,
            segment_index=int(row.segment_index),
            platform_message_ref=row.platform_message_ref,
            error_code=row.error_code,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_access_policy_record(
        row: ChannelAccessPolicyORM,
    ) -> ChannelAccessPolicyRecord:
        return ChannelAccessPolicyRecord(
            id=row.id,
            channel_instance_id=row.channel_instance_id,
            conversation_type=row.conversation_type,
            mode=row.mode,
            allowlist=list(row.allowlist or []),
            allow_commands=bool(row.allow_commands),
            updated_at=row.updated_at,
        )


def retention_cutoff(*, days: int, now: datetime | None = None) -> datetime:
    """入站去重记录的保留期截止点（保留期可配，见未决项 U5）。"""
    return (now or utcnow()) - timedelta(days=max(1, days))


__all__ = [
    "PROVISIONING_TERMINAL_STATUSES",
    "UNSET",
    "ChannelAccessPolicyRecord",
    "ChannelDeliveryRecord",
    "ChannelInstanceRecord",
    "ChannelProvisioningRecord",
    "ChannelRepository",
    "ChannelRouteRecord",
    "retention_cutoff",
]
