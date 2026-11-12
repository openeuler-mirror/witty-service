from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from witty_service.config import get_settings
from witty_service.main import create_app
from witty_service.sandbox.local_process import LocalProcessSandboxBackend


class FakeServices:
    def __init__(self) -> None:
        self.closed = False
        self.backend = None

    def get_sandbox_backend(self, sandbox_type: str):
        return self.backend

    async def close(self) -> None:
        self.closed = True


def test_create_app_serves_basic_endpoints_and_closes_services(monkeypatch) -> None:
    monkeypatch.setattr("witty_service.main.configure_logging", lambda: None)
    services = FakeServices()
    app = create_app(services=services)  # type: ignore[arg-type]

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/ping").json() == {"status": "ok"}
        capabilities = client.get("/server/capabilities").json()
        assert capabilities["supported_runtimes"]

    assert services.closed is True


def test_lifespan_reaps_orphan_servers(monkeypatch) -> None:
    monkeypatch.setattr("witty_service.main.configure_logging", lambda: None)
    services = FakeServices()
    services.backend = LocalProcessSandboxBackend()
    calls = []
    monkeypatch.setattr(
        services.backend,
        "reap_orphan_servers",
        lambda **kwargs: calls.append(kwargs) or [],
    )

    with TestClient(create_app(services=services)):  # type: ignore[arg-type]
        pass

    assert len(calls) == 1
    assert calls[0]["registered"] == {}
    assert (
        calls[0]["workspace_root"] == Path(get_settings().workspace.root).expanduser()
    )


def test_orphan_reap_failure_does_not_block_startup(monkeypatch) -> None:
    monkeypatch.setattr("witty_service.main.configure_logging", lambda: None)
    services = FakeServices()
    services.backend = LocalProcessSandboxBackend()

    def boom(**kwargs):
        raise OSError("cannot scan /proc")

    monkeypatch.setattr(services.backend, "reap_orphan_servers", boom)

    with TestClient(create_app(services=services)) as client:  # type: ignore[arg-type]
        assert client.get("/healthz").status_code == 200
