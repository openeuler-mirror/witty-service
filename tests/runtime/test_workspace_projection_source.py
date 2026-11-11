from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from witty_service.application.workspace_projection import (
    CatalogCredentialResolver,
    WorkspaceProjectionSource,
    WorkspaceProjectionStore,
)
from witty_service.domain.enums import AgentKind, SandboxType
from witty_service.persistence.entities import Base
from witty_service.persistence.repository import (
    AgentRepository,
    CatalogRepository,
    WorkspaceRepository,
)
from witty_service.storage.credentials import ModelCredentialStore

_NOW = 1_700_000_000


@pytest.fixture()
def repositories(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'witty.sqlite3'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield (
        AgentRepository(factory),
        CatalogRepository(factory),
        WorkspaceRepository(factory),
    )
    engine.dispose()


def _seed(agents, catalog, workspaces) -> tuple[str, str, str]:
    primary = agents.create("Primary", AgentKind.primary, _NOW)
    expert = agents.create("Expert", AgentKind.expert, _NOW)
    archived = agents.create("Retired", AgentKind.expert, _NOW)
    agents.archive(archived.id, _NOW)
    ref = ModelCredentialStore.ref_for_provider("provider-1", 3)
    enabled = catalog.create_model(
        "Model One",
        "provider-1",
        "model-one",
        None,
        "https://example.test/v1",
        ref,
        4096,
        0.2,
        _NOW,
        secret_revision=3,
    )
    disabled = catalog.create_model(
        "Model Two", "provider-1", "model-two", None, None, None, 2048, 0.0, _NOW
    )
    catalog.disable_model(disabled.id, _NOW)
    repo = catalog.create_skill_repo("local-repo", "local", _NOW)
    catalog.upsert_skill(
        repo.repo_id, "alpha", "alpha/SKILL.md", "a" * 64, {}, None, None, 1, _NOW
    )
    catalog.upsert_skill(
        repo.repo_id, "beta", "beta/SKILL.md", "b" * 64, {}, None, None, 0, _NOW
    )
    workspace = workspaces.create(
        "Default", "/srv/default", SandboxType.local, primary.id, enabled.id, 1, _NOW
    )
    return primary.id, expert.id, workspace.id


@pytest.mark.asyncio
async def test_projection_source_reads_only_visible_entries(repositories) -> None:
    agents, catalog, workspaces = repositories
    primary_id, expert_id, workspace_id = _seed(agents, catalog, workspaces)

    projection = await WorkspaceProjectionSource(agents=agents, catalog=catalog).load(
        workspace_id
    )

    assert [agent["name"] for agent in projection.document["agents"]] == [
        "Primary",
        "Expert",
    ]
    assert [agent["internal_name"] for agent in projection.document["agents"]] == [
        f"ag_{primary_id}",
        f"ag_{expert_id}",
    ]
    assert [model["provider_model_id"] for model in projection.document["models"]] == [
        "model-one"
    ]
    assert projection.document["models"][0]["secret_revision"] == 3
    assert [skill["name"] for skill in projection.document["skills"]] == ["alpha"]


@pytest.mark.asyncio
async def test_projection_store_round_trips_workspace_hash(repositories) -> None:
    agents, catalog, workspaces = repositories
    _primary, _expert, workspace_id = _seed(agents, catalog, workspaces)
    store = WorkspaceProjectionStore(workspaces)

    assert await store.get_published_hash(workspace_id) is None
    await store.save_published_hash(workspace_id, "hash-1")

    assert await store.get_published_hash(workspace_id) == "hash-1"
    assert await store.get_published_hash("missing") is None


@pytest.mark.asyncio
async def test_credential_resolver_reads_current_ref_only(
    repositories, tmp_path
) -> None:
    _agents, catalog, _workspaces = repositories
    store = ModelCredentialStore(tmp_path / "credentials")
    ref = ModelCredentialStore.ref_for_provider("provider-1", 3)
    store.write(ref, {"api_key": "sk-current"})
    catalog.create_model(
        "Model One",
        "provider-1",
        "model-one",
        None,
        None,
        ref,
        4096,
        0.2,
        _NOW,
        secret_revision=3,
    )
    resolver = CatalogCredentialResolver(catalog=catalog, store=store)

    assert await resolver.resolve(("provider-1", "provider-2")) == {
        "provider-1": "sk-current"
    }
