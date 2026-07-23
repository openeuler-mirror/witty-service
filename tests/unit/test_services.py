from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from witty_service.api import services as services_module
from witty_service.api.services import ServiceContainer
from witty_service.adapter.http_client import AdaptorHttpClient
from witty_service.config import (
    CorsSettings,
    DatabaseSettings,
    DockerSettings,
    InsightSettings,
    LoggingSettings,
    OpenClawGatewaySettings,
    OpenClawSettings,
    OpenCodeSettings,
    RuntimeSettings,
    Settings,
    WorkspaceSettings,
)
from witty_service.domain.errors import DomainError


class BackendStub:
    pass


def test_service_container_initializes_session_manager() -> None:
    repository = MagicMock()
    container = ServiceContainer(repository=repository, workspace_store=MagicMock())

    assert container.session_manager._repository is repository


def test_get_sandbox_backend_reuses_existing_backend() -> None:
    backend = BackendStub()
    container = ServiceContainer(
        repository=MagicMock(),
        workspace_store=MagicMock(),
        sandbox_backends={"docker": backend},
    )

    assert container.get_sandbox_backend("Docker") is backend


def test_get_sandbox_backend_creates_missing_backend(monkeypatch) -> None:
    backend = BackendStub()
    monkeypatch.setattr(
        services_module,
        "create_sandbox_backend",
        lambda sandbox_type: backend,
    )
    container = ServiceContainer(repository=MagicMock(), workspace_store=MagicMock())

    assert container.get_sandbox_backend("local_process") is backend
    assert container.sandbox_backends["local_process"] is backend


def test_get_agent_manager_for_agent_requires_existing_agent() -> None:
    repository = MagicMock()
    repository.get_agent.return_value = None
    container = ServiceContainer(repository=repository, workspace_store=MagicMock())

    with pytest.raises(DomainError) as exc_info:
        container.get_agent_manager_for_agent("missing")

    assert exc_info.value.code == services_module.AGENT_NOT_FOUND


def test_get_agent_manager_for_agent_uses_agent_sandbox(monkeypatch) -> None:
    backend = BackendStub()
    repository = MagicMock()
    repository.get_agent.return_value = SimpleNamespace(sandbox_type="docker")
    monkeypatch.setattr(
        services_module,
        "create_sandbox_backend",
        lambda sandbox_type: backend,
    )
    container = ServiceContainer(repository=repository, workspace_store=MagicMock())

    manager = container.get_agent_manager_for_agent("agent-1")

    assert manager._repository is repository
    assert manager._sandbox_backend is backend


def test_get_insight_http_client_returns_injected_client() -> None:
    insight_http_client = MagicMock(spec=AdaptorHttpClient)
    container = ServiceContainer(
        repository=MagicMock(),
        workspace_store=MagicMock(),
        insight_http_client=insight_http_client,
    )

    assert container.get_insight_http_client() is insight_http_client


def test_get_insight_http_client_requires_enabled_insight() -> None:
    container = ServiceContainer(repository=MagicMock(), workspace_store=MagicMock())

    with pytest.raises(DomainError) as exc_info:
        container.get_insight_http_client()

    assert exc_info.value.code == "INSIGHT_DISABLED"


def test_get_insight_facade_reuses_single_instance() -> None:
    container = ServiceContainer(repository=MagicMock(), workspace_store=MagicMock())

    first = container.get_insight_facade()
    second = container.get_insight_facade()

    assert first is second


def test_ensure_dir_exists_creates_sqlite_parent(tmp_path) -> None:
    db_path = tmp_path / "nested" / "witty.db"

    services_module._ensure_dir_exists(f"sqlite:///{db_path}")
    services_module._ensure_dir_exists("postgresql://example")

    assert db_path.parent.exists()


def test_build_default_services_creates_insight_http_client_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("WITTY_INSIGHT_ENABLED", "true")
    monkeypatch.setenv("WITTY_INSIGHT_BASE_URL", "http://127.0.0.1:7396")
    monkeypatch.setenv("WITTY_INSIGHT_TIMEOUT_SECONDS", "7.5")
    monkeypatch.setenv("WITTY_INSIGHT_BEARER_TOKEN", "secret-token")
    monkeypatch.setattr(services_module, "create_sqlite_engine", lambda *_args, **_kwargs: MagicMock())
    monkeypatch.setattr(services_module, "init_db", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(services_module, "create_session_factory", lambda *_args, **_kwargs: MagicMock())

    container = services_module.build_default_services()

    assert isinstance(container.insight_http_client, AdaptorHttpClient)
    assert container.insight_http_client.base_url == "http://127.0.0.1:7396"
    assert container.insight_http_client._timeout == 7.5
    assert container.insight_http_client._default_headers == {
        "Authorization": "Bearer secret-token"
    }


def test_build_default_services_passes_database_auto_create_flag(monkeypatch, tmp_path) -> None:
    engine = MagicMock()
    captured: dict[str, object] = {}
    settings = Settings(
        auth_token="test-token",
        cors=CorsSettings(),
        database=DatabaseSettings(url="sqlite:///:memory:", auto_create=False),
        logging=LoggingSettings(),
        docker=DockerSettings(),
        openclaw_gateway=OpenClawGatewaySettings(),
        workspace=WorkspaceSettings(root=str(tmp_path)),
        openclaw=OpenClawSettings(),
        insight=InsightSettings(enabled=False),
        opencode=OpenCodeSettings(),
        runtime=RuntimeSettings(),
    )

    monkeypatch.setattr(services_module, "get_settings", lambda: settings)
    monkeypatch.setattr(services_module, "create_sqlite_engine", lambda *_args, **_kwargs: engine)
    monkeypatch.setattr(
        services_module,
        "init_db",
        lambda _engine, **kwargs: captured.update(engine=_engine, **kwargs),
    )
    monkeypatch.setattr(services_module, "create_session_factory", lambda *_args, **_kwargs: MagicMock())

    services_module.build_default_services()

    assert captured == {"engine": engine, "auto_create": False}


@pytest.mark.asyncio
async def test_service_container_close_closes_insight_http_client() -> None:
    insight_http_client = MagicMock(spec=AdaptorHttpClient)
    insight_http_client.close = AsyncMock()
    container = ServiceContainer(
        repository=MagicMock(),
        workspace_store=MagicMock(),
        insight_http_client=insight_http_client,
    )

    await container.close()

    insight_http_client.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_service_container_close_is_noop_without_insight_http_client() -> None:
    container = ServiceContainer(repository=MagicMock(), workspace_store=MagicMock())

    await container.close()
