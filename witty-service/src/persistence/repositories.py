from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.domain.enums import AgentStatus
from src.persistence.orm import (
    AgentORM,
    AgentRuntimeStateORM,
    MessageEventORM,
    MessageORM,
    SessionORM,
    SessionStatus,
)
from src.sandbox.base import SandboxHandle


@dataclass(slots=True)
class AgentRecord:
    id: str
    name: str
    description: str
    sandbox_type: str
    adapter_type: str
    status: AgentStatus
    sandbox_id: str | None
    workspace_path: str
    idle_timeout_seconds: int
    has_scheduled_tasks: bool
    last_active_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class SessionRecord:
    id: str
    agent_id: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class SandboxStateRecord:
    agent_id: str
    sandbox_payload_json: dict[str, Any]
    adapter_base_url: str | None
    adapter_ready: bool
    last_error: str | None

    @property
    def handle(self) -> SandboxHandle:
        payload = dict(self.sandbox_payload_json)
        return SandboxHandle(
            sandbox_id=str(payload["sandbox_id"]),
            agent_id=str(payload.get("agent_id", self.agent_id)),
            workspace_path=str(payload["workspace_path"]),
            metadata=dict(payload.get("metadata", {})),
        )


class SqliteRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create_agent(
        self,
        *,
        name: str,
        sandbox_type: str,
        adapter_type: str,
        workspace_path: str,
        idle_timeout_seconds: int,
        description: str = "",
        status: AgentStatus | str = AgentStatus.creating,
        sandbox_id: str | None = None,
        has_scheduled_tasks: bool = False,
        last_active_at: datetime | None = None,
    ) -> AgentRecord:
        return self.create_agent_with_id(
            agent_id=str(uuid4()),
            name=name,
            sandbox_type=sandbox_type,
            adapter_type=adapter_type,
            workspace_path=workspace_path,
            idle_timeout_seconds=idle_timeout_seconds,
            description=description,
            status=status,
            sandbox_id=sandbox_id,
            has_scheduled_tasks=has_scheduled_tasks,
            last_active_at=last_active_at,
        )

    def create_agent_with_id(
        self,
        *,
        agent_id: str,
        name: str,
        sandbox_type: str,
        adapter_type: str,
        workspace_path: str,
        idle_timeout_seconds: int,
        description: str = "",
        status: AgentStatus | str = AgentStatus.creating,
        sandbox_id: str | None = None,
        has_scheduled_tasks: bool = False,
        last_active_at: datetime | None = None,
    ) -> AgentRecord:
        with self._session_factory() as session:
            row = AgentORM(
                id=agent_id,
                name=name,
                description=description,
                sandbox_type=sandbox_type,
                adapter_type=adapter_type,
                status=self._serialize_status(status),
                sandbox_id=sandbox_id,
                workspace_path=workspace_path,
                idle_timeout_seconds=idle_timeout_seconds,
                has_scheduled_tasks=has_scheduled_tasks,
                last_active_at=last_active_at,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_agent_record(row)

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        with self._session_factory() as session:
            row = session.get(AgentORM, agent_id)
            if row is None:
                return None
            return self._to_agent_record(row)

    def list_agents(self) -> list[AgentRecord]:
        with self._session_factory() as session:
            rows = session.query(AgentORM).order_by(AgentORM.created_at.asc()).all()
            return [self._to_agent_record(row) for row in rows]

    def update_agent_status(
        self,
        agent_id: str,
        status: AgentStatus | str,
        updated_at: datetime | None = None,
    ) -> AgentRecord:
        with self._session_factory() as session:
            row = session.get(AgentORM, agent_id)
            if row is None:
                raise KeyError(f"Agent not found: {agent_id}")
            row.status = self._serialize_status(status)
            if updated_at is not None:
                row.updated_at = updated_at
            session.commit()
            session.refresh(row)
            return self._to_agent_record(row)

    def create_session(
        self,
        agent_id: str,
        *,
        status: SessionStatus | str = SessionStatus.active,
    ) -> SessionRecord:
        with self._session_factory() as session:
            row = SessionORM(id=str(uuid4()), agent_id=agent_id, status=status)
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._to_session_record(row)

    def get_session(self, session_id: str) -> SessionRecord | None:
        with self._session_factory() as session:
            row = session.get(SessionORM, session_id)
            if row is None:
                return None
            return self._to_session_record(row)

    def list_sessions(self, agent_id: str) -> list[SessionRecord]:
        with self._session_factory() as session:
            rows = (
                session.query(SessionORM)
                .filter(SessionORM.agent_id == agent_id)
                .order_by(SessionORM.created_at.asc())
                .all()
            )
            return [self._to_session_record(row) for row in rows]

    def upsert_session(
        self,
        session_id: str,
        agent_id: str,
        status: str,
        context_initialized: bool = False,
        runtime_type: str | None = None,
        created_at: datetime | None = None,
    ) -> SessionRecord:
        """Upsert session from witty-agent-server"""
        with self._session_factory() as session:
            existing = session.get(SessionORM, session_id)
            now = datetime.now(timezone.utc)

            if existing is None:
                row = SessionORM(
                    id=session_id,
                    agent_id=agent_id,
                    status=SessionStatus(status),
                    created_at=created_at or now,
                    updated_at=now,
                )
                session.add(row)
            else:
                existing.status = SessionStatus(status)
                existing.updated_at = now

            session.commit()
            session.refresh(existing or row)
            return self._to_session_record(existing or row)

    def delete_session(self, session_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(SessionORM, session_id)
            if row is None:
                return
            session.delete(row)
            session.commit()

    def save_sandbox_state(
        self,
        agent_id: str,
        sandbox_payload_json: dict[str, Any],
        adapter_base_url: str | None,
        adapter_ready: bool = False,
        last_error: str | None = None,
    ) -> SandboxStateRecord:
        with self._session_factory() as session:
            row = session.get(AgentRuntimeStateORM, agent_id)
            if row is None:
                row = AgentRuntimeStateORM(agent_id=agent_id)
                session.add(row)
            row.runtime_payload_json = dict(sandbox_payload_json)
            row.adapter_base_url = adapter_base_url
            row.adapter_ready = adapter_ready
            row.last_error = last_error
            session.commit()
            session.refresh(row)
            return self._to_sandbox_state_record(row)

    def get_sandbox_state(self, agent_id: str) -> SandboxStateRecord | None:
        with self._session_factory() as session:
            row = session.get(AgentRuntimeStateORM, agent_id)
            if row is None:
                return None
            return self._to_sandbox_state_record(row)

    def create_message(
        self,
        agent_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> str:
        with self._session_factory() as session:
            row = MessageORM(
                id=str(uuid4()),
                agent_id=agent_id,
                session_id=session_id,
                role=role,
                content=content,
                metadata_json=dict(metadata_json or {}),
            )
            session.add(row)
            session.commit()
            return row.id

    def create_message_event_with_retry(
        self,
        *,
        agent_id: str,
        session_id: str,
        event_type: str,
        payload_json: dict[str, Any],
        seq_no: int,
        message_id: str | None = None,
        max_retries: int = 5,
    ) -> tuple[str, int]:
        next_seq_no = seq_no
        last_error: IntegrityError | None = None

        for _ in range(max_retries):
            try:
                event_id = self._create_message_event_once(
                    agent_id=agent_id,
                    session_id=session_id,
                    event_type=event_type,
                    payload_json=payload_json,
                    seq_no=next_seq_no,
                    message_id=message_id,
                )
                return event_id, next_seq_no
            except IntegrityError as exc:
                if not self._is_message_event_seq_conflict(exc):
                    raise
                last_error = exc
                next_seq_no = self._get_next_message_event_seq(session_id=session_id)

        if last_error is not None:
            raise last_error
        raise RuntimeError("Failed to create message event with retry.")

    def create_assistant_message_and_bind_events(
        self,
        *,
        agent_id: str,
        session_id: str,
        content: str,
        event_ids: list[str],
        metadata_json: dict[str, Any] | None = None,
    ) -> str:
        with self._session_factory() as session:
            row = MessageORM(
                id=str(uuid4()),
                agent_id=agent_id,
                session_id=session_id,
                role="assistant",
                content=content,
                metadata_json=dict(metadata_json or {}),
            )
            session.add(row)
            session.flush()
            if event_ids:
                session.query(MessageEventORM).filter(MessageEventORM.id.in_(event_ids)).update(
                    {MessageEventORM.message_id: row.id},
                    synchronize_session=False,
                )
            session.commit()
            return row.id

    def _get_next_message_event_seq(self, *, session_id: str) -> int:
        with self._session_factory() as session:
            max_seq_no = (
                session.query(func.max(MessageEventORM.seq_no))
                .filter(MessageEventORM.session_id == session_id)
                .scalar()
            )
            return int(max_seq_no or 0) + 1

    def _create_message_event_once(
        self,
        *,
        agent_id: str,
        session_id: str,
        event_type: str,
        payload_json: dict[str, Any],
        seq_no: int,
        message_id: str | None = None,
    ) -> str:
        with self._session_factory() as session:
            row = MessageEventORM(
                id=str(uuid4()),
                agent_id=agent_id,
                session_id=session_id,
                message_id=message_id,
                event_type=event_type,
                payload_json=dict(payload_json),
                seq_no=seq_no,
            )
            session.add(row)
            session.commit()
            return row.id

    @staticmethod
    def _is_message_event_seq_conflict(exc: IntegrityError) -> bool:
        message = str(exc.orig if exc.orig is not None else exc)
        return (
            "uq_message_events_session_seq" in message
            or "message_events.session_id, message_events.seq_no" in message
        )

    def delete_agent(self, agent_id: str) -> None:
        with self._session_factory() as session:
            row = session.get(AgentORM, agent_id)
            if row is None:
                return
            session.delete(row)
            session.commit()

    @staticmethod
    def _serialize_status(status: AgentStatus | str) -> str:
        return status.value if isinstance(status, AgentStatus) else AgentStatus(status).value

    @staticmethod
    def _to_agent_record(row: AgentORM) -> AgentRecord:
        return AgentRecord(
            id=row.id,
            name=row.name,
            description=row.description,
            sandbox_type=row.sandbox_type,
            adapter_type=row.adapter_type,
            status=AgentStatus(row.status),
            sandbox_id=row.sandbox_id,
            workspace_path=row.workspace_path,
            idle_timeout_seconds=row.idle_timeout_seconds,
            has_scheduled_tasks=row.has_scheduled_tasks,
            last_active_at=row.last_active_at,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_session_record(row: SessionORM) -> SessionRecord:
        return SessionRecord(
            id=row.id,
            agent_id=row.agent_id,
            status=row.status.value if isinstance(row.status, SessionStatus) else row.status,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_sandbox_state_record(row: AgentRuntimeStateORM) -> SandboxStateRecord:
        return SandboxStateRecord(
            agent_id=row.agent_id,
            sandbox_payload_json=dict(row.runtime_payload_json or {}),
            adapter_base_url=row.adapter_base_url,
            adapter_ready=row.adapter_ready,
            last_error=row.last_error,
        )
