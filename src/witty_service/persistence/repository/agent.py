from __future__ import annotations

from witty_service.domain.enums import AgentKind, AgentSourceType
from witty_service.domain.records import AgentDefinitionRecord
from witty_service.persistence.entities import AgentDefinitionORM
from witty_service.persistence.repository._base import RepositoryBase, new_id, record_of


class AgentRepository(RepositoryBase):
    def create(
        self,
        name: str,
        kind: AgentKind,
        description: str,
        system_prompt: str,
        source_type: AgentSourceType,
        source_ref: str | None,
        now: int,
    ) -> AgentDefinitionRecord:
        with self._transaction() as session:
            row = AgentDefinitionORM(
                id=new_id(),
                kind=kind.value,
                name=name,
                description=description,
                system_prompt=system_prompt,
                config_updated_at=now,
                source_type=source_type.value,
                source_ref=source_ref,
                archived_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return record_of(AgentDefinitionRecord, row)

    def get(self, agent_id: str) -> AgentDefinitionRecord | None:
        with self._read() as session:
            row = session.get(AgentDefinitionORM, agent_id)
            return None if row is None else record_of(AgentDefinitionRecord, row)

    def get_primary(self) -> AgentDefinitionRecord | None:
        with self._read() as session:
            row = (
                session.query(AgentDefinitionORM)
                .filter(
                    AgentDefinitionORM.kind == AgentKind.primary.value,
                    AgentDefinitionORM.archived_at.is_(None),
                )
                .one_or_none()
            )
            return None if row is None else record_of(AgentDefinitionRecord, row)

    def find_primary_any_state(self) -> AgentDefinitionRecord | None:
        with self._read() as session:
            row = (
                session.query(AgentDefinitionORM)
                .filter(
                    AgentDefinitionORM.kind == AgentKind.primary.value,
                    AgentDefinitionORM.archived_at.is_(None),
                )
                .one_or_none()
            )
            if row is None:
                row = (
                    session.query(AgentDefinitionORM)
                    .filter(
                        AgentDefinitionORM.kind == AgentKind.primary.value,
                        AgentDefinitionORM.archived_at.is_not(None),
                    )
                    .order_by(
                        AgentDefinitionORM.updated_at.desc(),
                        AgentDefinitionORM.created_at.desc(),
                    )
                    .first()
                )
            return None if row is None else record_of(AgentDefinitionRecord, row)

    def restore_primary(self, agent_id: str, now: int) -> AgentDefinitionRecord | None:
        with self._transaction() as session:
            row = session.get(AgentDefinitionORM, agent_id)
            if row is None or row.kind != AgentKind.primary.value:
                return None
            row.archived_at = None
            row.updated_at = now
            session.flush()
            return record_of(AgentDefinitionRecord, row)

    def list_catalog(self, include_archived: bool) -> list[AgentDefinitionRecord]:
        with self._read() as session:
            query = session.query(AgentDefinitionORM)
            if not include_archived:
                query = query.filter(AgentDefinitionORM.archived_at.is_(None))
            rows = query.order_by(
                AgentDefinitionORM.kind,
                AgentDefinitionORM.archived_at.is_(None).desc(),
                AgentDefinitionORM.name,
            ).all()
            return [record_of(AgentDefinitionRecord, row) for row in rows]

    def update_configuration(
        self,
        agent_id: str,
        expected_config_updated_at: int,
        name: str | None,
        description: str | None,
        system_prompt: str | None,
        now: int,
    ) -> AgentDefinitionRecord | None:
        with self._transaction() as session:
            row = session.get(AgentDefinitionORM, agent_id)
            if row is None or row.config_updated_at != expected_config_updated_at:
                return None
            changes = {
                "name": name,
                "description": description,
                "system_prompt": system_prompt,
            }
            for field, value in changes.items():
                if value is not None:
                    setattr(row, field, value)
            row.config_updated_at = now
            row.updated_at = now
            session.flush()
            return record_of(AgentDefinitionRecord, row)

    def archive(self, agent_id: str, now: int) -> AgentDefinitionRecord | None:
        with self._transaction() as session:
            row = session.get(AgentDefinitionORM, agent_id)
            if row is None:
                return None
            if row.kind == AgentKind.primary.value:
                raise ValueError("primary agent cannot be archived")
            if row.archived_at is None:
                row.archived_at = now
                row.updated_at = now
                session.flush()
            return record_of(AgentDefinitionRecord, row)
