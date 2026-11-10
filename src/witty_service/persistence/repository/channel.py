from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert

from witty_service.domain.enums import (
    ChannelAccessMode,
    ChannelConversationType,
    ChannelDeliveryCertainty,
    ChannelInstanceStatus,
    ChannelProvisioningStatus,
)
from witty_service.domain.records import (
    ChannelAccessPolicyRecord,
    ChannelDeliveryRecord,
    ChannelInstanceRecord,
    ChannelProvisioningRecord,
    ChannelRouteRecord,
)
from witty_service.persistence.entities import (
    ChannelAccessPolicyORM,
    ChannelDeliveryORM,
    ChannelInboundEventORM,
    ChannelInstanceORM,
    ChannelProvisioningORM,
    ChannelRouteORM,
)
from witty_service.persistence.repository._base import RepositoryBase, new_id, record_of


class ChannelRepository(RepositoryBase):
    def create_instance(
        self,
        channel: str,
        workspace_id: str | None,
        agent_id: str | None,
        model_id: str | None,
        display_name: str | None,
        owner_ref: str | None,
        config_json: dict[str, object],
        credential_ref: str | None,
        credential_mask: str | None,
        now: int,
    ) -> ChannelInstanceRecord:
        with self._transaction() as session:
            row = ChannelInstanceORM(
                id=new_id(),
                channel=channel,
                display_name=display_name,
                owner_ref=owner_ref,
                workspace_id=workspace_id,
                agent_id=agent_id,
                model_id=model_id,
                status=ChannelInstanceStatus.pending.value,
                paused=0,
                reason=None,
                generation=1,
                config_json=config_json,
                credential_ref=credential_ref,
                credential_mask=credential_mask,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return record_of(ChannelInstanceRecord, row)

    def get_instance(self, instance_id: str) -> ChannelInstanceRecord | None:
        with self._read() as session:
            row = session.get(ChannelInstanceORM, instance_id)
            return None if row is None else record_of(ChannelInstanceRecord, row)

    def list_instances(self, channel: str | None) -> list[ChannelInstanceRecord]:
        with self._read() as session:
            query = session.query(ChannelInstanceORM)
            if channel is not None:
                query = query.filter(ChannelInstanceORM.channel == channel)
            rows = query.order_by(
                ChannelInstanceORM.created_at.desc(), ChannelInstanceORM.id
            ).all()
            return [record_of(ChannelInstanceRecord, row) for row in rows]

    def update_instance_status(
        self,
        instance_id: str,
        status: ChannelInstanceStatus,
        paused: int,
        reason: str | None,
        now: int,
    ) -> ChannelInstanceRecord | None:
        with self._transaction() as session:
            row = session.get(ChannelInstanceORM, instance_id)
            if row is None:
                return None
            row.status = status.value
            row.paused = paused
            row.reason = reason
            row.updated_at = now
            session.flush()
            return record_of(ChannelInstanceRecord, row)

    def create_provisioning(
        self,
        channel: str,
        owner_ref: str | None,
        workspace_id: str | None,
        agent_id: str | None,
        model_id: str | None,
        expires_at: int,
        qr_content: str | None,
        poll_interval_ms: int,
        state_ref: str | None,
        now: int,
    ) -> ChannelProvisioningRecord:
        with self._transaction() as session:
            row = ChannelProvisioningORM(
                id=new_id(),
                channel=channel,
                owner_ref=owner_ref,
                workspace_id=workspace_id,
                agent_id=agent_id,
                model_id=model_id,
                status=ChannelProvisioningStatus.waiting.value,
                qr_content=qr_content,
                poll_interval_ms=poll_interval_ms,
                expires_at=expires_at,
                state_ref=state_ref,
                error_code=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return record_of(ChannelProvisioningRecord, row)

    def get_provisioning(
        self, provisioning_id: str
    ) -> ChannelProvisioningRecord | None:
        with self._read() as session:
            row = session.get(ChannelProvisioningORM, provisioning_id)
            return None if row is None else record_of(ChannelProvisioningRecord, row)

    def find_waiting_provisioning(
        self, channel: str, owner_ref: str | None
    ) -> ChannelProvisioningRecord | None:
        with self._read() as session:
            row = (
                session.query(ChannelProvisioningORM)
                .filter(
                    ChannelProvisioningORM.channel == channel,
                    ChannelProvisioningORM.owner_ref.is_(None)
                    if owner_ref is None
                    else ChannelProvisioningORM.owner_ref == owner_ref,
                    ChannelProvisioningORM.status
                    == ChannelProvisioningStatus.waiting.value,
                )
                .order_by(ChannelProvisioningORM.created_at.desc())
                .first()
            )
            return None if row is None else record_of(ChannelProvisioningRecord, row)

    def finish_provisioning(
        self,
        provisioning_id: str,
        status: ChannelProvisioningStatus,
        error_code: str | None,
        now: int,
    ) -> ChannelProvisioningRecord | None:
        if status == ChannelProvisioningStatus.waiting:
            raise ValueError("finish_provisioning requires a terminal status")
        with self._transaction() as session:
            row = session.get(ChannelProvisioningORM, provisioning_id)
            if row is None or row.status != ChannelProvisioningStatus.waiting.value:
                return None
            row.status = status.value
            row.error_code = error_code
            row.updated_at = now
            session.flush()
            return record_of(ChannelProvisioningRecord, row)

    def get_or_create_route(
        self,
        channel_instance_id: str,
        conversation_type: ChannelConversationType,
        platform_user_id: str,
        now: int,
    ) -> ChannelRouteRecord:
        with self._transaction() as session:
            statement = insert(ChannelRouteORM).values(
                id=new_id(),
                channel_instance_id=channel_instance_id,
                conversation_type=conversation_type.value,
                platform_user_id=platform_user_id,
                active_session_id=None,
                active_placeholder_ref=None,
                created_at=now,
                updated_at=now,
            )
            session.execute(
                statement.on_conflict_do_nothing(
                    index_elements=[
                        "channel_instance_id",
                        "conversation_type",
                        "platform_user_id",
                    ]
                )
            )
            row = (
                session.query(ChannelRouteORM)
                .filter(
                    ChannelRouteORM.channel_instance_id == channel_instance_id,
                    ChannelRouteORM.conversation_type == conversation_type.value,
                    ChannelRouteORM.platform_user_id == platform_user_id,
                )
                .one()
            )
            return record_of(ChannelRouteRecord, row)

    def set_route_session(
        self,
        route_id: str,
        session_id: str | None,
        placeholder_ref: str | None,
        now: int,
    ) -> ChannelRouteRecord | None:
        with self._transaction() as session:
            row = session.get(ChannelRouteORM, route_id)
            if row is None:
                return None
            row.active_session_id = session_id
            row.active_placeholder_ref = placeholder_ref
            row.updated_at = now
            session.flush()
            return record_of(ChannelRouteRecord, row)

    def record_inbound_event(
        self, channel_instance_id: str, platform_event_id: str, now: int
    ) -> bool:
        with self._transaction() as session:
            result = session.execute(
                insert(ChannelInboundEventORM)
                .values(
                    id=new_id(),
                    channel_instance_id=channel_instance_id,
                    platform_event_id=platform_event_id,
                    received_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=["channel_instance_id", "platform_event_id"]
                )
            )
            return result.rowcount == 1

    def create_delivery(
        self,
        channel_instance_id: str,
        certainty: ChannelDeliveryCertainty,
        route_id: str | None,
        session_id: str | None,
        turn_id: str | None,
        segment_index: int,
        platform_message_ref: str | None,
        error_code: str | None,
        now: int,
    ) -> ChannelDeliveryRecord:
        with self._transaction() as session:
            row = ChannelDeliveryORM(
                id=new_id(),
                channel_instance_id=channel_instance_id,
                route_id=route_id,
                session_id=session_id,
                turn_id=turn_id,
                certainty=certainty.value,
                segment_index=segment_index,
                platform_message_ref=platform_message_ref,
                error_code=error_code,
                created_at=now,
            )
            session.add(row)
            session.flush()
            return record_of(ChannelDeliveryRecord, row)

    def get_access_policy(
        self, channel_instance_id: str, conversation_type: ChannelConversationType
    ) -> ChannelAccessPolicyRecord | None:
        with self._read() as session:
            row = (
                session.query(ChannelAccessPolicyORM)
                .filter(
                    ChannelAccessPolicyORM.channel_instance_id == channel_instance_id,
                    ChannelAccessPolicyORM.conversation_type == conversation_type.value,
                )
                .one_or_none()
            )
            return None if row is None else record_of(ChannelAccessPolicyRecord, row)

    def upsert_access_policy(
        self,
        channel_instance_id: str,
        conversation_type: ChannelConversationType,
        mode: ChannelAccessMode,
        allowlist_json: list[object],
        allow_commands: int,
        now: int,
    ) -> ChannelAccessPolicyRecord:
        with self._transaction() as session:
            statement = insert(ChannelAccessPolicyORM).values(
                id=new_id(),
                channel_instance_id=channel_instance_id,
                conversation_type=conversation_type.value,
                mode=mode.value,
                allowlist_json=allowlist_json,
                allow_commands=allow_commands,
                updated_at=now,
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=["channel_instance_id", "conversation_type"],
                    set_={
                        "mode": statement.excluded.mode,
                        "allowlist_json": statement.excluded.allowlist_json,
                        "allow_commands": statement.excluded.allow_commands,
                        "updated_at": now,
                    },
                )
            )
            row = (
                session.query(ChannelAccessPolicyORM)
                .filter(
                    ChannelAccessPolicyORM.channel_instance_id == channel_instance_id,
                    ChannelAccessPolicyORM.conversation_type == conversation_type.value,
                )
                .one()
            )
            return record_of(ChannelAccessPolicyRecord, row)
