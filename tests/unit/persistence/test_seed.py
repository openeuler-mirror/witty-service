from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session, sessionmaker

from witty_service.domain.enums import AgentKind, SandboxType
from witty_service.persistence.entities import AgentDefinitionORM, Base
from witty_service.persistence.repository import (
    AgentRepository,
    CatalogRepository,
    WorkspaceRepository,
)
from witty_service.persistence.seed import (
    BUILTIN_SKILL_REPO_NAME,
    seed_defaults,
)


@pytest.fixture()
def session_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def test_seed_creates_primary_builtin_repo_and_default_workspace(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    directory = tmp_path / "workspace"
    result = seed_defaults(session_factory, host_directory=directory)

    assert result.primary_agent.kind is AgentKind.primary
    assert result.primary_agent.archived_at is None
    assert result.builtin_skill_repo.repo_name == BUILTIN_SKILL_REPO_NAME
    assert result.default_workspace.is_default == 1
    assert result.default_workspace.host_directory == str(directory)
    assert result.default_workspace.default_agent_id == result.primary_agent.id
    assert result.default_workspace.default_model_id is None
    assert directory.is_dir()


def test_seed_reuses_rows_and_keeps_existing_default_workspace(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    catalog = CatalogRepository(session_factory)
    model = catalog.create_model(
        "Model", "provider", "model-1", None, None, None, 8192, 0.7, 1_000
    )
    catalog.set_default_model(model.id, 1_001)

    first = seed_defaults(session_factory, host_directory=tmp_path / "first")
    second = seed_defaults(session_factory, host_directory=tmp_path / "second")

    assert second.primary_agent.id == first.primary_agent.id
    assert second.builtin_skill_repo.repo_id == first.builtin_skill_repo.repo_id
    assert second.default_workspace.id == first.default_workspace.id
    assert second.default_workspace.default_model_id == model.id
    assert (
        len(AgentRepository(session_factory).list_catalog(include_archived=True)) == 1
    )
    assert len(WorkspaceRepository(session_factory).list_sidebar()) == 1
    assert not (tmp_path / "second").exists()


def test_seed_promotes_existing_workspace_with_same_directory(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    primary = AgentRepository(session_factory).create(
        "Primary", AgentKind.primary, 1_000
    )
    existing = WorkspaceRepository(session_factory).create(
        "Existing",
        str(tmp_path / "workspace"),
        SandboxType.local,
        primary.id,
        None,
        0,
        1_000,
    )

    result = seed_defaults(session_factory, host_directory=tmp_path / "workspace")

    assert result.default_workspace.id == existing.id
    assert result.default_workspace.is_default == 1


def test_seed_prefers_active_primary_when_archived_primary_exists(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    agents = AgentRepository(session_factory)
    archived = agents.create("Primary", AgentKind.primary, 1_000)
    with session_factory() as session:
        session.execute(
            update(AgentDefinitionORM)
            .where(AgentDefinitionORM.id == archived.id)
            .values(archived_at=1_001)
        )
        session.commit()
    replacement = agents.create("Replacement", AgentKind.primary, 1_002)

    result = seed_defaults(session_factory, host_directory=tmp_path / "workspace")

    assert result.primary_agent.id == replacement.id
    assert result.primary_agent.archived_at is None
    assert result.default_workspace.default_agent_id == replacement.id


def test_seed_restores_archived_primary(
    tmp_path: Path, session_factory: sessionmaker[Session]
) -> None:
    first = seed_defaults(session_factory, host_directory=tmp_path / "workspace")
    with session_factory() as session:
        session.execute(
            update(AgentDefinitionORM)
            .where(AgentDefinitionORM.id == first.primary_agent.id)
            .values(archived_at=1_000)
        )
        session.commit()

    result = seed_defaults(session_factory, host_directory=tmp_path / "workspace")

    assert result.primary_agent.id == first.primary_agent.id
    assert result.primary_agent.archived_at is None
