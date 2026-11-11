from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from witty_agent_server.infra.clients.opencode_v2_client import (
    OpenCodeV2Client,
    OpenCodeV2Error,
)
from witty_agent_server.runtime_adapter import OpenCodeRuntimeAdapter
from witty_service.runtime.contracts import (
    EnsureSessionRequest,
    RuntimeConnection,
    RuntimeLocationVerification,
    RuntimeModelRef,
    RuntimeWorkspace,
)
from witty_service.runtime.errors import RuntimeUnavailableError

_BASE_URL = "http://runtime.test"


def _json(status: int, body: object) -> httpx.Response:
    return httpx.Response(
        status, json=body, headers={"content-type": "application/json"}
    )


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> OpenCodeV2Client:
    return OpenCodeV2Client(_BASE_URL, transport=httpx.MockTransport(handler))


def _adapter(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    startup_timeout: float = 30.0,
    poll_interval: float = 0.2,
) -> OpenCodeRuntimeAdapter:
    return OpenCodeRuntimeAdapter(
        client_factory=lambda _url: _client(handler),
        startup_timeout=startup_timeout,
        poll_interval=poll_interval,
    )


def _connection() -> RuntimeConnection:
    return RuntimeConnection(
        workspace=RuntimeWorkspace(workspace_id="ws_a", runtime_directory="/srv/ws_a"),
        base_url=_BASE_URL,
    )


@pytest.mark.asyncio
async def test_connect_requires_healthy_runtime() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/health"
        return _json(200, {"healthy": False})

    adapter = _adapter(handler, startup_timeout=0)

    with pytest.raises(RuntimeUnavailableError):
        await adapter.connect(_connection())

    health = await adapter.health("ws_a")
    assert health.ready is False


@pytest.mark.asyncio
async def test_connect_polls_until_healthy() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return _json(200, {"healthy": attempts >= 3})

    adapter = _adapter(handler, startup_timeout=5, poll_interval=0.01)

    health = await adapter.connect(_connection())

    assert health.ready is True
    assert attempts == 3


@pytest.mark.asyncio
async def test_connect_and_ensure_session() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/health":
            return _json(200, {"healthy": True})
        if request.method == "POST" and request.url.path == "/api/session":
            return _json(201, {"data": {"id": "ses_1"}})
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    adapter = _adapter(handler)
    health = await adapter.connect(_connection())

    session = await adapter.ensure_session(
        EnsureSessionRequest(
            workspace=_connection().workspace, session_id="session_local"
        )
    )

    assert health.ready is True
    assert session.runtime_session_id == "ses_1"
    assert session.session_id == "session_local"
    assert calls == [
        ("GET", "/api/health"),
        ("POST", "/api/session"),
    ]


@pytest.mark.asyncio
async def test_ensure_session_reuses_persisted_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/health":
            return _json(200, {"healthy": True})
        if request.url.path == "/api/session/ses_existing":
            return _json(200, {"data": {"id": "ses_existing"}})
        raise AssertionError(f"unexpected request {request.url.path}")

    adapter = _adapter(handler)
    await adapter.connect(_connection())

    session = await adapter.ensure_session(
        EnsureSessionRequest(
            workspace=_connection().workspace,
            session_id="session_local",
            runtime_session_id="ses_existing",
        )
    )

    assert session.runtime_session_id == "ses_existing"


@pytest.mark.asyncio
async def test_ensure_session_recreates_missing_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/health":
            return _json(200, {"healthy": True})
        if request.url.path == "/api/session/ses_gone":
            return _json(404, {"_tag": "SessionNotFoundError", "message": "gone"})
        if request.method == "POST" and request.url.path == "/api/session":
            return _json(201, {"data": {"id": "ses_new"}})
        raise AssertionError(f"unexpected request {request.url.path}")

    adapter = _adapter(handler)
    await adapter.connect(_connection())

    session = await adapter.ensure_session(
        EnsureSessionRequest(
            workspace=_connection().workspace,
            session_id="session_local",
            runtime_session_id="ses_gone",
        )
    )

    assert session.runtime_session_id == "ses_new"


@pytest.mark.asyncio
async def test_inspect_workspace_checks_location_catalog() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/health":
            return _json(200, {"healthy": True})
        if path == "/api/location":
            assert request.url.params["location[directory]"] == "/srv/ws_a"
            return _json(200, {"directory": "/srv/ws_a", "project": {"id": "global"}})
        if path == "/api/agent":
            return _json(200, {"location": {}, "data": [{"name": "build"}]})
        if path == "/api/model":
            return _json(
                200,
                {
                    "location": {},
                    "data": [{"id": "m1", "providerID": "p1"}],
                },
            )
        if path == "/api/skill":
            return _json(200, {"location": {}, "data": []})
        raise AssertionError(f"unexpected request {path}")

    adapter = _adapter(handler)
    await adapter.connect(_connection())

    snapshot = await adapter.inspect_workspace("ws_a")

    assert snapshot.health.ready is True
    assert snapshot.location.ready is True
    assert snapshot.location.agents == ("build",)
    assert snapshot.location.models[0].provider_id == "p1"


@pytest.mark.asyncio
async def test_verify_location_rejects_empty_requirements_and_unconnected() -> None:
    adapter = OpenCodeRuntimeAdapter()

    empty = await adapter.verify_location(RuntimeLocationVerification("ws_a"))
    assert empty.ready is False
    assert empty.detail == "projection has no agents or models"

    disconnected = await adapter.verify_location(
        RuntimeLocationVerification(
            "ws_a", agents=("build",), models=(RuntimeModelRef("p1", "m1"),)
        )
    )
    assert disconnected.ready is False
    assert disconnected.detail == "workspace runtime is not connected"


@pytest.mark.asyncio
async def test_verify_location_waits_for_step_started() -> None:
    started = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal started
        path = request.url.path
        if path == "/api/health":
            return _json(200, {"healthy": True})
        if path == "/api/location":
            return _json(200, {"directory": "/srv/ws_a"})
        if path == "/api/agent":
            return _json(200, {"location": {}, "data": [{"name": "build"}]})
        if path == "/api/model":
            return _json(
                200, {"location": {}, "data": [{"id": "m1", "providerID": "p1"}]}
            )
        if path == "/api/skill":
            return _json(200, {"location": {}, "data": []})
        if path == "/api/session" and request.method == "POST":
            return _json(200, {"data": {"id": "ses_1"}})
        if path == "/api/session/ses_1/prompt":
            started = True
            return _json(200, {"data": {}})
        if path == "/api/session/ses_1/history":
            if not started:
                return _json(200, {"data": [], "hasMore": False})
            return _json(
                200,
                {
                    "data": [
                        {
                            "id": "evt_1",
                            "type": "session.next.step.started",
                            "data": {
                                "sessionID": "ses_1",
                                "assistantMessageID": "msg_1",
                                "timestamp": 1,
                            },
                            "durable": {"seq": 1},
                        }
                    ],
                    "hasMore": False,
                },
            )
        raise AssertionError(f"unexpected request {request.method} {path}")

    adapter = _adapter(handler, poll_interval=0.01)
    await adapter.connect(_connection())

    readiness = await adapter.verify_location(
        RuntimeLocationVerification(
            "ws_a",
            agents=("build",),
            models=(RuntimeModelRef(provider_id="p1", model_id="m1"),),
            timeout=1.0,
        )
    )

    assert readiness.ready is True
    assert readiness.agents == ("build",)


@pytest.mark.asyncio
async def test_verify_location_times_out_without_step_started() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/health":
            return _json(200, {"healthy": True})
        if path == "/api/location":
            return _json(200, {"directory": "/srv/ws_a"})
        if path == "/api/agent":
            return _json(200, {"location": {}, "data": [{"name": "build"}]})
        if path == "/api/model":
            return _json(
                200, {"location": {}, "data": [{"id": "m1", "providerID": "p1"}]}
            )
        if path == "/api/skill":
            return _json(200, {"location": {}, "data": []})
        if path == "/api/session" and request.method == "POST":
            return _json(200, {"data": {"id": "ses_1"}})
        if path == "/api/session/ses_1/prompt":
            return _json(200, {"data": {}})
        if path == "/api/session/ses_1/history":
            return _json(200, {"data": [], "hasMore": False})
        raise AssertionError(f"unexpected request {request.method} {path}")

    adapter = _adapter(handler, poll_interval=0.01)
    await adapter.connect(_connection())

    readiness = await adapter.verify_location(
        RuntimeLocationVerification(
            "ws_a",
            agents=("build",),
            models=(RuntimeModelRef(provider_id="p1", model_id="m1"),),
            timeout=0.05,
        )
    )

    assert readiness.ready is False
    assert readiness.detail == "warm-up turn did not start at the location"


@pytest.mark.asyncio
async def test_empty_catalog_keeps_location_not_ready() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/health":
            return _json(200, {"healthy": True})
        if path == "/api/location":
            return _json(200, {"directory": "/srv/ws_a"})
        if path in {"/api/agent", "/api/model", "/api/skill"}:
            return _json(200, {"location": {}, "data": []})
        raise AssertionError(f"unexpected request {path}")

    adapter = _adapter(handler)
    await adapter.connect(_connection())

    snapshot = await adapter.inspect_workspace("ws_a")

    assert snapshot.health.ready is True
    assert snapshot.location.ready is False
    assert snapshot.location.detail == "location catalog is not ready"


@pytest.mark.asyncio
async def test_ensure_session_rejects_another_directory() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/health":
            return _json(200, {"healthy": True})
        raise AssertionError(f"unexpected request {request.url.path}")

    adapter = _adapter(handler)
    await adapter.connect(_connection())

    with pytest.raises(RuntimeUnavailableError, match="does not match"):
        await adapter.ensure_session(
            EnsureSessionRequest(
                workspace=RuntimeWorkspace(
                    workspace_id="ws_a", runtime_directory="/srv/other"
                ),
                session_id="session_local",
            )
        )


@pytest.mark.asyncio
async def test_html_fallback_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, text="<html></html>", headers={"content-type": "text/html"}
        )

    client = _client(handler)

    with pytest.raises(OpenCodeV2Error, match="expected JSON"):
        await client.get_location(directory="/srv/ws_a")
