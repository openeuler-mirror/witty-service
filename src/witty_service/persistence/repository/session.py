from __future__ import annotations

from sqlalchemy import func, update
from sqlalchemy.dialects.sqlite import insert

from witty_service.domain.enums import (
    MessageRole,
    SessionOrigin,
    TurnOperationStatus,
    TurnStatus,
)
from witty_service.domain.records import (
    SessionRecord,
    TurnOperationRecord,
    TurnRecord,
    TurnRuntimeMessageRecord,
)
from witty_service.persistence.entities import (
    AgentDefinitionORM,
    ModelORM,
    SessionORM,
    TurnOperationORM,
    TurnORM,
    TurnRuntimeMessageORM,
    WorkspaceORM,
)
from witty_service.persistence.repository._base import RepositoryBase, new_id, record_of


class SessionRepository(RepositoryBase):
    def create(
        self,
        workspace_id: str,
        agent_id: str,
        model_id: str | None,
        origin: SessionOrigin,
        title: str | None,
        scheduled_task_id: str | None,
        runtime_session_id: str | None,
        now: int,
    ) -> SessionRecord:
        with self._transaction() as session:
            row = SessionORM(
                id=new_id(),
                workspace_id=workspace_id,
                current_agent_id=agent_id,
                current_model_id=model_id,
                runtime_session_id=runtime_session_id,
                last_turn_seq=0,
                title=title,
                pinned=0,
                origin=origin.value,
                scheduled_task_id=scheduled_task_id,
                last_activity_at=now,
                deleted_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.query(WorkspaceORM).filter(WorkspaceORM.id == workspace_id).update(
                {
                    "session_count": WorkspaceORM.session_count + 1,
                    "last_activity_at": func.max(WorkspaceORM.last_activity_at, now),
                    "updated_at": now,
                },
                synchronize_session=False,
            )
            session.flush()
            return record_of(SessionRecord, row)

    def get(self, session_id: str) -> SessionRecord | None:
        with self._read() as session:
            row = session.get(SessionORM, session_id)
            return None if row is None else record_of(SessionRecord, row)

    def list_by_workspace(
        self, workspace_id: str, include_deleted: bool
    ) -> list[SessionRecord]:
        with self._read() as session:
            query = session.query(SessionORM).filter(
                SessionORM.workspace_id == workspace_id
            )
            if not include_deleted:
                query = query.filter(SessionORM.deleted_at.is_(None))
            rows = query.order_by(
                SessionORM.pinned.desc(),
                SessionORM.last_activity_at.desc(),
                SessionORM.id.desc(),
            ).all()
            return [record_of(SessionRecord, row) for row in rows]

    def count_open_sessions(self, workspace_id: str) -> int:
        with self._read() as session:
            return (
                session.query(SessionORM)
                .filter(
                    SessionORM.workspace_id == workspace_id,
                    SessionORM.deleted_at.is_(None),
                )
                .count()
            )

    def update_current_settings(
        self, session_id: str, agent_id: str, model_id: str | None, now: int
    ) -> SessionRecord | None:
        with self._transaction() as session:
            row = session.get(SessionORM, session_id)
            if row is None:
                return None
            row.current_agent_id = agent_id
            row.current_model_id = model_id
            row.updated_at = now
            session.flush()
            return record_of(SessionRecord, row)

    def set_runtime_session_id(
        self, session_id: str, runtime_session_id: str, now: int
    ) -> SessionRecord | None:
        with self._transaction() as session:
            row = session.get(SessionORM, session_id)
            if row is None:
                return None
            row.runtime_session_id = runtime_session_id
            row.updated_at = now
            session.flush()
            return record_of(SessionRecord, row)

    def set_title(self, session_id: str, title: str, now: int) -> SessionRecord | None:
        with self._transaction() as session:
            row = session.get(SessionORM, session_id)
            if row is None:
                return None
            row.title = title
            row.updated_at = now
            session.flush()
            return record_of(SessionRecord, row)

    def set_pinned(
        self, session_id: str, pinned: int, now: int
    ) -> SessionRecord | None:
        with self._transaction() as session:
            row = session.get(SessionORM, session_id)
            if row is None:
                return None
            row.pinned = pinned
            row.updated_at = now
            session.flush()
            return record_of(SessionRecord, row)

    def mark_deleted(self, session_id: str, now: int) -> bool:
        with self._transaction() as session:
            row = session.get(SessionORM, session_id)
            if row is None or row.deleted_at is not None:
                return False
            row.deleted_at = now
            row.updated_at = now
            session.query(WorkspaceORM).filter(
                WorkspaceORM.id == row.workspace_id
            ).update(
                {
                    "session_count": func.max(WorkspaceORM.session_count - 1, 0),
                    "updated_at": now,
                },
                synchronize_session=False,
            )
            return True

    def start_turn(
        self, session_id: str, agent_id: str, model_id: str, now: int
    ) -> TurnRecord:
        with self._transaction() as session:
            agent = session.get(AgentDefinitionORM, agent_id)
            if agent is None or agent.archived_at is not None:
                raise LookupError(f"agent {agent_id} is not available")
            model = session.get(ModelORM, model_id)
            if model is None or model.enabled != 1:
                raise LookupError(f"model {model_id} is not available")

            next_seq = session.execute(
                update(SessionORM)
                .where(
                    SessionORM.id == session_id,
                    SessionORM.deleted_at.is_(None),
                )
                .values(
                    last_turn_seq=SessionORM.last_turn_seq + 1,
                    last_activity_at=func.max(SessionORM.last_activity_at, now),
                    updated_at=now,
                )
                .returning(SessionORM.last_turn_seq)
            ).scalar_one_or_none()
            if next_seq is None:
                raise LookupError(f"session {session_id} not found")

            row = TurnORM(
                id=new_id(),
                session_id=session_id,
                turn_seq=next_seq,
                agent_id=agent_id,
                model_id=model_id,
                agent_name_snapshot=agent.name,
                model_name_snapshot=model.display_name,
                status=TurnStatus.running.value,
                error_code=None,
                error_message=None,
                started_at=now,
                finished_at=None,
                created_at=now,
            )
            session.add(row)
            session.flush()
            return record_of(TurnRecord, row)

    def get_turn(self, turn_id: str) -> TurnRecord | None:
        with self._read() as session:
            row = session.get(TurnORM, turn_id)
            return None if row is None else record_of(TurnRecord, row)

    def list_turns(self, session_id: str) -> list[TurnRecord]:
        with self._read() as session:
            rows = (
                session.query(TurnORM)
                .filter(TurnORM.session_id == session_id)
                .order_by(TurnORM.turn_seq)
                .all()
            )
            return [record_of(TurnRecord, row) for row in rows]

    def finish_turn(
        self,
        turn_id: str,
        status: TurnStatus,
        error_code: str | None,
        error_message: str | None,
        now: int,
    ) -> TurnRecord | None:
        if status == TurnStatus.running:
            raise ValueError("turn must be finished with a terminal status")
        with self._transaction() as session:
            updated = (
                session.query(TurnORM)
                .filter(
                    TurnORM.id == turn_id,
                    TurnORM.status == TurnStatus.running.value,
                )
                .update(
                    {
                        "status": status.value,
                        "error_code": error_code,
                        "error_message": error_message,
                        "finished_at": now,
                    },
                    synchronize_session=False,
                )
            )
            if updated != 1:
                return None
            row = session.get(TurnORM, turn_id)
            session.query(SessionORM).filter(SessionORM.id == row.session_id).update(
                {
                    "last_activity_at": func.max(SessionORM.last_activity_at, now),
                    "updated_at": now,
                },
                synchronize_session=False,
            )
            workspace_id = (
                session.query(SessionORM.workspace_id)
                .filter(SessionORM.id == row.session_id)
                .scalar()
            )
            session.query(WorkspaceORM).filter(WorkspaceORM.id == workspace_id).update(
                {
                    "last_activity_at": func.max(WorkspaceORM.last_activity_at, now),
                    "updated_at": now,
                },
                synchronize_session=False,
            )
            session.flush()
            return record_of(TurnRecord, row)

    def create_operation(
        self, session_id: str, target_turn_id: str, now: int
    ) -> TurnOperationRecord:
        with self._transaction() as session:
            target = session.get(TurnORM, target_turn_id)
            if target is None or target.session_id != session_id:
                raise LookupError(
                    f"turn {target_turn_id} does not belong to {session_id}"
                )
            row = TurnOperationORM(
                id=new_id(),
                session_id=session_id,
                target_turn_id=target_turn_id,
                type="regenerate",
                status=TurnOperationStatus.reverting.value,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return record_of(TurnOperationRecord, row)

    def get_operation(self, operation_id: str) -> TurnOperationRecord | None:
        with self._read() as session:
            row = session.get(TurnOperationORM, operation_id)
            return None if row is None else record_of(TurnOperationRecord, row)

    def finish_operation(
        self, operation_id: str, status: TurnOperationStatus, now: int
    ) -> TurnOperationRecord | None:
        if status == TurnOperationStatus.reverting:
            raise ValueError("operation must be finished with a terminal status")
        with self._transaction() as session:
            updated = (
                session.query(TurnOperationORM)
                .filter(
                    TurnOperationORM.id == operation_id,
                    TurnOperationORM.status == TurnOperationStatus.reverting.value,
                )
                .update(
                    {"status": status.value, "updated_at": now},
                    synchronize_session=False,
                )
            )
            if updated != 1:
                return None
            return record_of(
                TurnOperationRecord, session.get(TurnOperationORM, operation_id)
            )

    def add_runtime_message(
        self,
        runtime_message_id: str,
        turn_id: str,
        role: MessageRole,
        ordinal: int,
        now: int,
    ) -> TurnRuntimeMessageRecord:
        with self._transaction() as session:
            statement = insert(TurnRuntimeMessageORM).values(
                runtime_message_id=runtime_message_id,
                turn_id=turn_id,
                role=role.value,
                ordinal=ordinal,
                created_at=now,
            )
            result = session.execute(
                statement.on_conflict_do_nothing(index_elements=["runtime_message_id"])
            )
            row = session.get(TurnRuntimeMessageORM, runtime_message_id)
            if result.rowcount == 0 and (
                row.turn_id != turn_id
                or row.role != role.value
                or row.ordinal != ordinal
            ):
                raise ValueError(
                    f"runtime message {runtime_message_id} is already mapped differently"
                )
            return record_of(TurnRuntimeMessageRecord, row)

    def list_runtime_messages(self, turn_id: str) -> list[TurnRuntimeMessageRecord]:
        with self._read() as session:
            rows = (
                session.query(TurnRuntimeMessageORM)
                .filter(TurnRuntimeMessageORM.turn_id == turn_id)
                .order_by(TurnRuntimeMessageORM.ordinal)
                .all()
            )
            return [record_of(TurnRuntimeMessageRecord, row) for row in rows]
