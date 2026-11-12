from __future__ import annotations

import asyncio
from collections.abc import Mapping

from witty_service.persistence.entities.base import utc_ms_now
from witty_service.persistence.repository.agent import AgentRepository
from witty_service.persistence.repository.catalog import CatalogRepository
from witty_service.persistence.repository.workspace import WorkspaceRepository
from witty_service.runtime.projection import (
    ProjectionAgent,
    ProjectionInputs,
    ProjectionModel,
    ProjectionSkill,
    RuntimeProjection,
    build_projection,
)
from witty_service.storage.credentials import ModelCredentialStore


class WorkspaceProjectionSource:
    """从仓储读取全量非密输入并生成投影；只在发布开始时调用一次。"""

    def __init__(self, *, agents: AgentRepository, catalog: CatalogRepository) -> None:
        self._agents = agents
        self._catalog = catalog

    async def load(self, workspace_id: str) -> RuntimeProjection:
        return await asyncio.to_thread(self._load)

    def _load(self) -> RuntimeProjection:
        return build_projection(
            ProjectionInputs(
                agents=tuple(
                    ProjectionAgent(
                        definition_id=agent.id,
                        name=agent.name,
                        description=agent.description,
                        system_prompt=agent.system_prompt,
                        kind=agent.kind.value,
                        source_type=agent.source_type.value,
                        source_ref=agent.source_ref,
                    )
                    for agent in self._agents.list_catalog(include_archived=False)
                ),
                models=tuple(
                    ProjectionModel(
                        model_id=model.id,
                        display_name=model.display_name,
                        provider_id=model.provider_id,
                        provider_model_id=model.provider_model_id,
                        max_tokens=model.max_tokens,
                        temperature=model.temperature,
                        is_default=bool(model.is_default),
                        secret_revision=model.secret_revision,
                        compatibility=model.compatibility,
                        api_base_url=model.api_base_url,
                    )
                    for model in self._catalog.list_models(include_disabled=False)
                ),
                skills=tuple(
                    ProjectionSkill(
                        skill_name=skill.skill_name,
                        content_sha256=skill.content_sha256,
                    )
                    for skill in self._catalog.list_enabled_skills()
                ),
            )
        )


class WorkspaceProjectionStore:
    """发布 hash 落在 Workspace 聚合上；不新增配置版本表。"""

    def __init__(self, workspaces: WorkspaceRepository) -> None:
        self._workspaces = workspaces

    async def get_published_hash(self, workspace_id: str) -> str | None:
        record = await asyncio.to_thread(self._workspaces.get, workspace_id)
        return None if record is None else record.published_projection_hash

    async def save_published_hash(
        self, workspace_id: str, projection_hash: str
    ) -> None:
        await asyncio.to_thread(
            self._workspaces.set_published_projection_hash,
            workspace_id,
            projection_hash,
            utc_ms_now(),
        )


class CatalogCredentialResolver:
    """按 provider 解析当前修订的凭据本体；仅供发布写配置时使用。"""

    def __init__(
        self, *, catalog: CatalogRepository, store: ModelCredentialStore
    ) -> None:
        self._catalog = catalog
        self._store = store

    async def resolve(self, provider_ids: tuple[str, ...]) -> Mapping[str, str]:
        return await asyncio.to_thread(self._resolve, provider_ids)

    def _resolve(self, provider_ids: tuple[str, ...]) -> dict[str, str]:
        credentials: dict[str, str] = {}
        for provider_id in provider_ids:
            credential_ref, _ = self._catalog.get_provider_credential(provider_id)
            if credential_ref is None:
                continue
            api_key = self._store.resolve_api_key(credential_ref)
            if api_key:
                credentials[provider_id] = api_key
        return credentials
