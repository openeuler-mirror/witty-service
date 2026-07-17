from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from witty_agent_server.application.services.agent.errors import AgentServiceError
from witty_agent_server.application.services.agent.opencode_agent_service import (
    OpenCodeAgentService,
)
from witty_agent_server.application.services.agent.opencode_lifecycle_service import (
    OpenCodeLifecycleService,
)
from witty_agent_server.infra.clients.opencode_client import (
    OpenCodeClient,
    OpenCodeClientError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _temp_workspace_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Set WITTY_WORKSPACE_ROOT to a temp directory for each test."""
    monkeypatch.setenv("WITTY_WORKSPACE_ROOT", str(tmp_path))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _lifecycle_with_handler(handler, *, profile: str = "test-profile") -> OpenCodeLifecycleService:
    client = OpenCodeClient(serve_port=4096, username="u", password="p")
    client._http_client = httpx.Client(
        base_url=client.server_url,
        auth=("u", "p"),
        transport=httpx.MockTransport(handler),
        timeout=3.0,
    )
    return OpenCodeLifecycleService(client=client, profile=profile)


def _service_with_lifecycle(handler, *, profile: str = "test-profile") -> OpenCodeAgentService:
    lifecycle = _lifecycle_with_handler(handler, profile=profile)
    client = lifecycle.client
    return OpenCodeAgentService(lifecycle_service=lifecycle, client=client)


# ---------------------------------------------------------------------------
# setup_mcp - parameter validation
# ---------------------------------------------------------------------------


def test_setup_mcp_missing_name_raises() -> None:
    svc = OpenCodeAgentService()
    with pytest.raises(AgentServiceError) as exc:
        svc.setup_mcp(mcp_server_name=None, mcp_server_config={"type": "local"})
    assert exc.value.status_code == 400
    assert exc.value.code == "OPENCODE_MCP_CONFIG_INVALID"


def test_setup_mcp_missing_config_raises() -> None:
    svc = OpenCodeAgentService()
    with pytest.raises(AgentServiceError) as exc:
        svc.setup_mcp(mcp_server_name="test", mcp_server_config=None)
    assert exc.value.status_code == 400


def test_setup_mcp_non_dict_config_raises() -> None:
    svc = OpenCodeAgentService()
    with pytest.raises(AgentServiceError):
        svc.setup_mcp(mcp_server_name="test", mcp_server_config="not-a-dict")


# ---------------------------------------------------------------------------
# setup_mcp - success path
# ---------------------------------------------------------------------------


def test_setup_mcp_success() -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method, "path": request.url.path})
        if request.url.path == "/mcp" and request.method == "POST":
            return httpx.Response(200, json={"status": "connected"})
        if request.url.path == "/mcp/test-mcp/disconnect":
            return httpx.Response(404)  # first time, not connected
        return httpx.Response(200, json={})

    svc = _service_with_lifecycle(handler)
    svc.setup_mcp(mcp_server_name="test-mcp", mcp_server_config={"type": "local"})

    # HTTP API calls: disconnect (best-effort), post_mcp_add
    paths = [c["path"] for c in calls]
    assert "/mcp/test-mcp/disconnect" in paths
    assert "/mcp" in paths  # POST /mcp

    # Disk write: verify config was persisted to XDG file (XDG_CONFIG_HOME is workspace)
    config_path = svc._lifecycle_service._opencode_config_path()
    assert config_path is not None
    assert config_path.is_file()
    disk_cfg = json.loads(config_path.read_text())
    assert "mcp" in disk_cfg
    assert "test-mcp" in disk_cfg["mcp"]


# ---------------------------------------------------------------------------
# setup_mcp - serve not running (persist only)
# ---------------------------------------------------------------------------


def test_setup_mcp_serve_not_running_persists_only() -> None:
    """When serve is not running, disk write still succeeds (HTTP calls are best-effort)."""
    mcp_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        mcp_calls.append({"method": request.method, "path": request.url.path})
        if request.url.path == "/mcp/test-mcp/disconnect":
            return httpx.Response(404)
        if request.url.path == "/mcp" and request.method == "POST":
            return httpx.Response(200, json={"status": "connected"})
        return httpx.Response(200, json={})

    svc = _service_with_lifecycle(handler)
    svc.setup_mcp(mcp_server_name="test-mcp", mcp_server_config={"type": "local"})

    # Disk write should succeed even without serve running
    config_path = svc._lifecycle_service._opencode_config_path()
    assert config_path is not None
    assert config_path.is_file()
    disk_cfg = json.loads(config_path.read_text())
    assert "test-mcp" in disk_cfg.get("mcp", {})


# ---------------------------------------------------------------------------
# setup_mcp - http error conversion
# ---------------------------------------------------------------------------


def test_setup_mcp_http_error_converts() -> None:
    """HTTP errors during mcp_set are logged but do not raise (disk write succeeds first)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mcp" and request.method == "POST":
            return httpx.Response(500, text="mcp add failed")
        if request.url.path == "/mcp/test-mcp/disconnect":
            return httpx.Response(404)
        return httpx.Response(200, json={})

    svc = _service_with_lifecycle(handler)
    # Should NOT raise — disk write happens before HTTP, HTTP errors are caught
    svc.setup_mcp(mcp_server_name="test-mcp", mcp_server_config={"type": "local"})

    # Disk write should still have succeeded
    config_path = svc._lifecycle_service._opencode_config_path()
    assert config_path is not None
    assert config_path.is_file()
    disk_cfg = json.loads(config_path.read_text())
    assert "test-mcp" in disk_cfg.get("mcp", {})


# ---------------------------------------------------------------------------
# unset_mcp - parameter validation
# ---------------------------------------------------------------------------


def test_unset_mcp_missing_name_raises() -> None:
    svc = OpenCodeAgentService()
    with pytest.raises(AgentServiceError) as exc:
        svc.unset_mcp(mcp_server_name=None)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# unset_mcp - success
# ---------------------------------------------------------------------------


def test_unset_mcp_success() -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method, "path": request.url.path})
        if request.url.path == "/mcp/test-mcp/disconnect" and request.method == "POST":
            return httpx.Response(204)
        return httpx.Response(200, json={})

    svc = _service_with_lifecycle(handler)

    # First, write a config with the MCP server to disk
    config_path = svc._lifecycle_service._opencode_config_path()
    assert config_path is not None
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"mcp": {"test-mcp": {"type": "local"}}}))

    svc.unset_mcp(mcp_server_name="test-mcp")

    # HTTP disconnect should be called
    paths = [c["path"] for c in calls]
    assert "/mcp/test-mcp/disconnect" in paths

    # Disk config should no longer have the MCP server
    disk_cfg = json.loads(config_path.read_text())
    assert "test-mcp" not in disk_cfg.get("mcp", {})


# ---------------------------------------------------------------------------
# unset_mcp - http error conversion
# ---------------------------------------------------------------------------


def test_unset_mcp_http_error_converts() -> None:
    """HTTP errors during disconnect are caught and logged, not raised."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/mcp/test-mcp/disconnect":
            return httpx.Response(204)
        return httpx.Response(500, text="error")

    svc = _service_with_lifecycle(handler)

    # Write config with the MCP server
    config_path = svc._lifecycle_service._opencode_config_path()
    assert config_path is not None
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"mcp": {"test-mcp": {"type": "local"}}}))

    # Should NOT raise — disk write happens before HTTP
    svc.unset_mcp(mcp_server_name="test-mcp")

    # Disk config should have MCP server removed
    disk_cfg = json.loads(config_path.read_text())
    assert "test-mcp" not in disk_cfg.get("mcp", {})