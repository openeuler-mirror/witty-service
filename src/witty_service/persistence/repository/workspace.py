from __future__ import annotations

from sqlalchemy import func

from witty_service.domain.enums import SandboxType
from witty_service.domain.records import WorkspaceRecord
from witty_service.persistence.entities import WorkspaceORM
from witty_service.persistence.repository._base import RepositoryBase, new_id, record_of


class WorkspaceRepository(RepositoryBase):
    def create(
        self,
        name: str,
        host_directory: str,
        sandbox_type: SandboxType,
        default_agent_id: str,
        default_model_id: str | None,
        is_default: int,
        now: int,
    ) -> WorkspaceRecord:
        with self._transaction() as session:
            row = WorkspaceORM(
                id=new_id(),
                name=name,
                host_directory=host_directory,
                sandbox_type=sandbox_type.value,
                default_agent_id=default_agent_id,
                default_model_id=default_model_id,
                is_default=is_default,
                published_projection_hash=None,
                session_count=0,
                last_activity_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return record_of(WorkspaceRecord, row)

    def get(self, workspace_id: str) -> WorkspaceRecord | None:
        with self._read() as session:
            row = session.get(WorkspaceORM, workspace_id)
            return None if row is None else record_of(WorkspaceRecord, row)

    def get_by_host_directory(self, host_directory: str) -> WorkspaceRecord | None:
        with self._read() as session:
            row = (
                session.query(WorkspaceORM)
                .filter(WorkspaceORM.host_directory == host_directory)
                .one_or_none()
            )
            return None if row is None else record_of(WorkspaceRecord, row)

    def get_default(self) -> WorkspaceRecord | None:
        with self._read() as session:
            row = (
                session.query(WorkspaceORM)
                .filter(WorkspaceORM.is_default == 1)
                .one_or_none()
            )
            return None if row is None else record_of(WorkspaceRecord, row)

    def list_sidebar(self) -> list[WorkspaceRecord]:
        with self._read() as session:
            rows = (
                session.query(WorkspaceORM)
                .order_by(
                    WorkspaceORM.is_default.desc(),
                    WorkspaceORM.last_activity_at.desc(),
                    WorkspaceORM.id.desc(),
                )
                .all()
            )
            return [record_of(WorkspaceRecord, row) for row in rows]

    def update_metadata(
        self, workspace_id: str, name: str | None, now: int
    ) -> WorkspaceRecord | None:
        with self._transaction() as session:
            row = session.get(WorkspaceORM, workspace_id)
            if row is None:
                return None
            if name is not None:
                row.name = name
            row.updated_at = now
            session.flush()
            return record_of(WorkspaceRecord, row)

    def set_default(self, workspace_id: str, now: int) -> WorkspaceRecord | None:
        with self._transaction() as session:
            row = session.get(WorkspaceORM, workspace_id)
            if row is None:
                return None
            session.query(WorkspaceORM).filter(
                WorkspaceORM.is_default == 1, WorkspaceORM.id != workspace_id
            ).update({"is_default": 0, "updated_at": now}, synchronize_session=False)
            row.is_default = 1
            row.updated_at = now
            session.flush()
            return record_of(WorkspaceRecord, row)

    def update_defaults(
        self, workspace_id: str, agent_id: str, model_id: str | None, now: int
    ) -> WorkspaceRecord | None:
        with self._transaction() as session:
            row = session.get(WorkspaceORM, workspace_id)
            if row is None:
                return None
            row.default_agent_id = agent_id
            row.default_model_id = model_id
            row.updated_at = now
            session.flush()
            return record_of(WorkspaceRecord, row)

    def set_published_projection_hash(
        self, workspace_id: str, projection_hash: str | None, now: int
    ) -> WorkspaceRecord | None:
        with self._transaction() as session:
            row = session.get(WorkspaceORM, workspace_id)
            if row is None:
                return None
            row.published_projection_hash = projection_hash
            row.updated_at = now
            session.flush()
            return record_of(WorkspaceRecord, row)

    def touch_activity(self, workspace_id: str, now: int) -> None:
        with self._transaction() as session:
            session.query(WorkspaceORM).filter(WorkspaceORM.id == workspace_id).update(
                {
                    "last_activity_at": func.max(WorkspaceORM.last_activity_at, now),
                    "updated_at": now,
                },
                synchronize_session=False,
            )
