from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from witty_service.api.services import ServiceContainer, build_default_services


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
        workspace = container.workspaces.get_default()
        primary = container.agents.get_primary()

        assert workspace is not None
        assert workspace.host_directory == str(
            tmp_path / "root" / "workspaces" / "default"
        )
        assert primary is not None
        assert container.catalog_repository is not None
        assert container.workspace_store.base_dir == tmp_path / "root"
        assert container.sessions.get("missing-session") is None
        assert (
            container.sandbox_backends["local_process"].sandbox_type == "local_process"
        )
    finally:
        asyncio.run(container.close())


def _container(events: list[str], *, failing_runtime: bool = False) -> ServiceContainer:
    class Runtime:
        async def shutdown(self) -> None:
            events.append("runtime")
            if failing_runtime:
                raise RuntimeError("runtime shutdown failed")

    class Backend:
        def stop_all(self) -> None:
            events.append("backend")

    engine = MagicMock()
    engine.dispose.side_effect = lambda: events.append("engine")
    return ServiceContainer(
        engine=engine,
        session_factory=MagicMock(),
        catalog_repository=MagicMock(),
        workspace_store=MagicMock(),
        agents=MagicMock(),
        workspaces=MagicMock(),
        sessions=MagicMock(),
        runtime_manager=Runtime(),  # type: ignore[arg-type]
        sandbox_backends={"local_process": Backend()},  # type: ignore[dict-item]
    )


def test_close_order_is_runtime_backends_engine(settings_env: None) -> None:
    events: list[str] = []
    asyncio.run(_container(events).close())
    assert events == ["runtime", "backend", "engine"]


def test_close_still_disposes_engine_when_runtime_shutdown_fails(
    settings_env: None,
) -> None:
    events: list[str] = []
    with pytest.raises(RuntimeError):
        asyncio.run(_container(events, failing_runtime=True).close())
    assert events == ["runtime", "backend", "engine"]


def test_close_skips_extra_backends_when_configured(
    settings_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WITTY_STOP_SANDBOXES_ON_SHUTDOWN", "false")
    monkeypatch.setattr("witty_service.config._settings", None)
    events: list[str] = []
    asyncio.run(_container(events).close())
    assert events == ["runtime", "engine"]
