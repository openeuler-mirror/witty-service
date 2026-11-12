from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert

from witty_service.domain.enums import SkillDiscoverStatus
from witty_service.domain.records import (
    McpServerRecord,
    ModelRecord,
    SkillRecord,
    SkillRepoRecord,
)
from witty_service.persistence.entities import (
    ChannelInstanceORM,
    ChannelProvisioningORM,
    McpServerORM,
    ModelORM,
    ScheduledTaskORM,
    SessionORM,
    SkillORM,
    SkillRepoORM,
    TurnORM,
    WorkspaceORM,
)
from witty_service.persistence.repository._base import RepositoryBase, new_id, record_of


class CatalogRepository(RepositoryBase):
    def create_model(
        self,
        display_name: str,
        provider_id: str,
        provider_model_id: str,
        compatibility: str | None,
        api_base_url: str | None,
        credential_ref: str | None,
        max_tokens: int,
        temperature: float,
        now: int,
        *,
        secret_revision: int | None = None,
    ) -> ModelRecord:
        with self._transaction() as session:
            if credential_ref is None or secret_revision is None:
                existing = (
                    session.query(ModelORM)
                    .filter(
                        ModelORM.provider_id == provider_id,
                        ModelORM.credential_ref.isnot(None),
                    )
                    .order_by(ModelORM.created_at, ModelORM.id)
                    .first()
                )
                if existing is not None:
                    if credential_ref is None:
                        credential_ref = existing.credential_ref
                    if secret_revision is None:
                        secret_revision = existing.secret_revision
            row = ModelORM(
                id=new_id(),
                display_name=display_name,
                provider_id=provider_id,
                provider_model_id=provider_model_id,
                compatibility=compatibility,
                api_base_url=api_base_url,
                credential_ref=credential_ref,
                enabled=1,
                max_tokens=max_tokens,
                temperature=temperature,
                is_default=0,
                secret_revision=0 if secret_revision is None else secret_revision,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return record_of(ModelRecord, row)

    def get_model(self, model_id: str) -> ModelRecord | None:
        with self._read() as session:
            row = session.get(ModelORM, model_id)
            return None if row is None else record_of(ModelRecord, row)

    def list_models(self, include_disabled: bool) -> list[ModelRecord]:
        with self._read() as session:
            query = session.query(ModelORM)
            if not include_disabled:
                query = query.filter(ModelORM.enabled == 1)
            rows = query.order_by(
                ModelORM.is_default.desc(),
                ModelORM.created_at.desc(),
                ModelORM.id,
            ).all()
            return [record_of(ModelRecord, row) for row in rows]

    def get_default_model(self) -> ModelRecord | None:
        with self._read() as session:
            row = (
                session.query(ModelORM)
                .filter(ModelORM.is_default == 1, ModelORM.enabled == 1)
                .one_or_none()
            )
            return None if row is None else record_of(ModelRecord, row)

    def set_default_model(self, model_id: str, now: int) -> ModelRecord | None:
        with self._transaction() as session:
            row = session.get(ModelORM, model_id)
            if row is None or row.enabled != 1:
                return None
            session.query(ModelORM).filter(
                ModelORM.is_default == 1, ModelORM.id != model_id
            ).update({"is_default": 0, "updated_at": now}, synchronize_session=False)
            row.is_default = 1
            row.updated_at = now
            session.flush()
            return record_of(ModelRecord, row)

    def disable_model(self, model_id: str, now: int) -> ModelRecord | None:
        with self._transaction() as session:
            row = session.get(ModelORM, model_id)
            if row is None:
                return None
            row.enabled = 0
            row.is_default = 0
            row.updated_at = now
            session.flush()
            return record_of(ModelRecord, row)

    def list_provider_models(self, provider_id: str) -> list[ModelRecord]:
        with self._read() as session:
            rows = (
                session.query(ModelORM)
                .filter(ModelORM.provider_id == provider_id)
                .order_by(ModelORM.created_at, ModelORM.id)
                .all()
            )
            return [record_of(ModelRecord, row) for row in rows]

    def count_models_for_provider(self, provider_id: str) -> int:
        with self._read() as session:
            return (
                session.query(func.count())
                .select_from(ModelORM)
                .filter(ModelORM.provider_id == provider_id)
                .scalar()
                or 0
            )

    def get_provider_credential(self, provider_id: str) -> tuple[str | None, int]:
        with self._read() as session:
            row = (
                session.query(ModelORM)
                .filter(ModelORM.provider_id == provider_id)
                .order_by(ModelORM.created_at, ModelORM.id)
                .first()
            )
            return (
                (None, 0) if row is None else (row.credential_ref, row.secret_revision)
            )

    def replace_provider_credential(
        self,
        provider_id: str,
        new_ref: str | None,
        new_revision: int,
        now: int,
        *,
        expected_revision: int | None = None,
    ) -> int:
        with self._transaction() as session:
            query = session.query(ModelORM).filter(ModelORM.provider_id == provider_id)
            total = query.count()
            if total == 0:
                return 0
            if expected_revision is not None:
                matching = query.filter(
                    ModelORM.secret_revision == expected_revision
                ).count()
                if matching != total:
                    return 0
            return query.update(
                {
                    "credential_ref": new_ref,
                    "secret_revision": new_revision,
                    "updated_at": now,
                },
                synchronize_session=False,
            )

    def count_model_references(self, model_id: str) -> dict[str, int]:
        references = {
            "workspaces": WorkspaceORM.default_model_id,
            "sessions": SessionORM.current_model_id,
            "turns": TurnORM.model_id,
            "scheduled_tasks": ScheduledTaskORM.model_id,
            "channel_instances": ChannelInstanceORM.model_id,
            "channel_provisionings": ChannelProvisioningORM.model_id,
        }
        with self._read() as session:
            return {
                name: session.query(func.count())
                .select_from(column.class_)
                .filter(column == model_id)
                .scalar()
                or 0
                for name, column in references.items()
            }

    def create_skill_repo(
        self,
        repo_name: str,
        source_type: str,
        now: int,
        *,
        branch: str | None = None,
        url: str | None = None,
        local_path: str | None = None,
    ) -> SkillRepoRecord:
        with self._transaction() as session:
            row = SkillRepoORM(
                repo_id=new_id(),
                repo_name=repo_name,
                source_type=source_type,
                branch=branch,
                url=url,
                local_path=local_path,
                skill_discover_status=SkillDiscoverStatus.init.value,
                skill_count=0,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            session.flush()
            return record_of(SkillRepoRecord, row)

    def get_skill_repo(self, repo_id: str) -> SkillRepoRecord | None:
        with self._read() as session:
            row = session.get(SkillRepoORM, repo_id)
            return None if row is None else record_of(SkillRepoRecord, row)

    def get_skill_repo_by_name(self, repo_name: str) -> SkillRepoRecord | None:
        with self._read() as session:
            row = (
                session.query(SkillRepoORM)
                .filter(SkillRepoORM.repo_name == repo_name)
                .one_or_none()
            )
            return None if row is None else record_of(SkillRepoRecord, row)

    def list_skill_repos(self) -> list[SkillRepoRecord]:
        with self._read() as session:
            rows = session.query(SkillRepoORM).order_by(SkillRepoORM.repo_name).all()
            return [record_of(SkillRepoRecord, row) for row in rows]

    def upsert_skill(
        self,
        repo_id: str,
        skill_name: str,
        relative_path: str,
        content_sha256: str,
        metadata_json: dict[str, object],
        skill_source: str | None,
        skill_md_url: str | None,
        enabled: int,
        now: int,
    ) -> SkillRecord:
        with self._transaction() as session:
            statement = insert(SkillORM).values(
                skill_id=new_id(),
                repo_id=repo_id,
                skill_name=skill_name,
                relative_path=relative_path,
                content_sha256=content_sha256,
                metadata_json=metadata_json,
                skill_source=skill_source,
                skill_md_url=skill_md_url,
                enabled=enabled,
                created_at=now,
                updated_at=now,
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=["repo_id", "relative_path"],
                    set_={
                        "skill_name": statement.excluded.skill_name,
                        "content_sha256": statement.excluded.content_sha256,
                        "metadata_json": statement.excluded.metadata_json,
                        "skill_source": statement.excluded.skill_source,
                        "skill_md_url": statement.excluded.skill_md_url,
                        "enabled": statement.excluded.enabled,
                        "updated_at": now,
                    },
                )
            )
            row = (
                session.query(SkillORM)
                .filter(
                    SkillORM.repo_id == repo_id,
                    SkillORM.relative_path == relative_path,
                )
                .one()
            )
            skill_count = (
                session.query(func.count())
                .select_from(SkillORM)
                .filter(SkillORM.repo_id == repo_id)
                .scalar()
            )
            session.query(SkillRepoORM).filter(SkillRepoORM.repo_id == repo_id).update(
                {"skill_count": skill_count, "updated_at": now},
                synchronize_session=False,
            )
            return record_of(SkillRecord, row)

    def list_skills(self, repo_id: str) -> list[SkillRecord]:
        with self._read() as session:
            rows = (
                session.query(SkillORM)
                .filter(SkillORM.repo_id == repo_id)
                .order_by(SkillORM.skill_name)
                .all()
            )
            return [record_of(SkillRecord, row) for row in rows]

    def list_enabled_skills(self) -> list[SkillRecord]:
        with self._read() as session:
            rows = session.query(SkillORM).filter(SkillORM.enabled == 1).all()
            return sorted(
                (record_of(SkillRecord, row) for row in rows),
                key=lambda skill: (skill.skill_name.casefold(), skill.skill_name),
            )

    def upsert_mcp_server(
        self,
        mcp_server_name: str,
        config_json: dict[str, object],
        credential_ref: str | None,
        enabled: int,
        now: int,
    ) -> McpServerRecord:
        with self._transaction() as session:
            existing = (
                session.query(McpServerORM)
                .filter(McpServerORM.mcp_server_name == mcp_server_name)
                .one_or_none()
            )
            revision_increment = (
                0
                if existing is None or existing.credential_ref == credential_ref
                else 1
            )
            statement = insert(McpServerORM).values(
                id=new_id(),
                mcp_server_name=mcp_server_name,
                config_json=config_json,
                credential_ref=credential_ref,
                enabled=enabled,
                secret_revision=0,
                created_at=now,
                updated_at=now,
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=["mcp_server_name"],
                    set_={
                        "config_json": statement.excluded.config_json,
                        "credential_ref": statement.excluded.credential_ref,
                        "enabled": statement.excluded.enabled,
                        "secret_revision": McpServerORM.secret_revision
                        + revision_increment,
                        "updated_at": now,
                    },
                )
            )
            row = (
                session.query(McpServerORM)
                .populate_existing()
                .filter(McpServerORM.mcp_server_name == mcp_server_name)
                .one()
            )
            return record_of(McpServerRecord, row)

    def list_mcp_servers(self, include_disabled: bool) -> list[McpServerRecord]:
        with self._read() as session:
            query = session.query(McpServerORM)
            if not include_disabled:
                query = query.filter(McpServerORM.enabled == 1)
            rows = query.order_by(McpServerORM.mcp_server_name).all()
            return [record_of(McpServerRecord, row) for row in rows]
