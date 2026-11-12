from __future__ import annotations

from fastapi.testclient import TestClient

from witty_service.main import create_app


class FakeServices:
    def __init__(self) -> None:
        self.closed = False

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
