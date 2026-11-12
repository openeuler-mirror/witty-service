from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from witty_service.api.services import build_default_services
from witty_service.persistence.repository import AgentRepository, WorkspaceRepository


@pytest.fixture()
def settings_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "WITTY_DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'db.sqlite3'}"
    )
    monkeypatch.setenv("WITTY_WORKSPACE_ROOT", str(tmp_path / "root"))
    monkeypatch.setattr("witty_service.config._settings", None)


def test_build_default_services_seeds_database_and_exposes_repositories(
    tmp_path: Path, settings_env: None
) -> None:
    container = build_default_services()
    try:
        workspace = WorkspaceRepository(container.session_factory).get_default()
        primary = AgentRepository(container.session_factory).get_primary()

        assert workspace is not None
        assert workspace.host_directory == str(
            tmp_path / "root" / "workspaces" / "default"
        )
        assert primary is not None
        assert container.catalog_repository is not None
        assert container.workspace_store.base_dir == tmp_path / "root"
    finally:
        asyncio.run(container.close())
