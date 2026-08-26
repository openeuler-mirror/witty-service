from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from witty_service.api import mcp_servers as mcp_api
from witty_service.api.schemas import CreateMcpServerRequest, UpdateMcpServerRequest
from witty_service.domain.errors import DomainError
from witty_service.persistence.repositories import McpServerRecord


def _server_record(**overrides: object) -> McpServerRecord:
    now = datetime.now(UTC)
    data = {
        "id": "server-1",
        "mcp_server_name": "filesystem",
        "mcp_server_config": {"filesystem": {"command": "npx"}},
        "created_at": now,
        "updated_at": now,
    }
    data.update(overrides)
    return McpServerRecord(**data)


def _services() -> MagicMock:
    services = MagicMock()
    services.repository = MagicMock()
    return services


@pytest.mark.parametrize("config", [{}, []])
def test_extract_server_name_rejects_invalid_config(config) -> None:
    with pytest.raises(DomainError) as exc_info:
        mcp_api._extract_server_name(config)

    assert exc_info.value.code == mcp_api.MCP_SERVER_CONFIG_INVALID


def test_create_mcp_server_extracts_name_from_config() -> None:
    services = _services()
    services.repository.create_mcp_server.return_value = _server_record()

    resp = mcp_api.create_mcp_server(
        payload=CreateMcpServerRequest(
            mcp_server_config={"filesystem": {"command": "npx"}},
        ),
        services=services,
    )

    services.repository.create_mcp_server.assert_called_once_with(
        mcp_server_name="filesystem",
        mcp_server_config={"filesystem": {"command": "npx"}},
    )
    assert resp.id == "server-1"
    assert resp.mcp_server_name == "filesystem"


def test_create_cvekit_mcp_does_not_persist_tokens() -> None:
    services = _services()
    services.repository.create_mcp_server.return_value = _server_record(
        mcp_server_name="cvekit_mcp",
        mcp_server_config={"cvekit_mcp": {"command": "python"}},
    )

    mcp_api.create_mcp_server(
        payload=CreateMcpServerRequest(
            mcp_server_config={
                "cvekit_mcp": {
                    "command": "python",
                    "env": {
                        "CVEKIT_LOG_DIR": "/tmp/cvekit",
                        "GITCODE_TOKEN": "token",
                        "GITEE_TOKEN": "legacy",
                    },
                }
            }
        ),
        services=services,
    )

    persisted = services.repository.create_mcp_server.call_args.kwargs[
        "mcp_server_config"
    ]
    assert persisted["cvekit_mcp"]["env"] == {"CVEKIT_LOG_DIR": "/tmp/cvekit"}


def test_list_mcp_servers_returns_responses() -> None:
    services = _services()
    services.repository.list_mcp_servers.return_value = [_server_record()]

    resp = mcp_api.list_mcp_servers(services=services)

    assert [item.id for item in resp] == ["server-1"]


def test_delete_mcp_server_handles_missing_and_existing() -> None:
    services = _services()
    services.repository.get_mcp_server.return_value = None

    with pytest.raises(DomainError) as exc_info:
        mcp_api.delete_mcp_server("missing", services=services)
    assert exc_info.value.code == mcp_api.MCP_SERVER_NOT_FOUND

    services.repository.get_mcp_server.return_value = _server_record()
    resp = mcp_api.delete_mcp_server("server-1", services=services)

    assert resp.status_code == 204
    services.repository.delete_mcp_server.assert_called_once_with("server-1")


def test_update_mcp_server_extracts_name_from_config() -> None:
    services = _services()
    services.repository.get_mcp_server.return_value = _server_record()
    services.repository.update_mcp_server.return_value = _server_record(
        mcp_server_name="git",
        mcp_server_config={"git": {"command": "git-mcp"}},
    )

    resp = mcp_api.update_mcp_server(
        "server-1",
        payload=UpdateMcpServerRequest(
            mcp_server_config={"git": {"command": "git-mcp"}},
        ),
        services=services,
    )

    services.repository.update_mcp_server.assert_called_once_with(
        server_id="server-1",
        mcp_server_name="git",
        mcp_server_config={"git": {"command": "git-mcp"}},
    )
    assert resp.mcp_server_name == "git"


def test_update_flat_cvekit_mcp_config_keeps_server_name_and_strips_tokens() -> None:
    services = _services()
    services.repository.get_mcp_server.return_value = _server_record(
        mcp_server_name="cvekit_mcp",
        mcp_server_config={"command": "python"},
    )
    services.repository.update_mcp_server.return_value = _server_record(
        mcp_server_name="cvekit_mcp",
        mcp_server_config={"command": "python"},
    )

    mcp_api.update_mcp_server(
        "server-1",
        payload=UpdateMcpServerRequest(
            mcp_server_config={
                "command": "python",
                "env": {
                    "CVEKIT_LOG_DIR": "/tmp/cvekit",
                    "GITCODE_TOKEN": "token",
                },
            }
        ),
        services=services,
    )

    services.repository.update_mcp_server.assert_called_once_with(
        server_id="server-1",
        mcp_server_name="cvekit_mcp",
        mcp_server_config={
            "command": "python",
            "env": {"CVEKIT_LOG_DIR": "/tmp/cvekit"},
        },
    )


def test_update_mcp_server_raises_when_missing() -> None:
    services = _services()
    services.repository.get_mcp_server.return_value = None

    with pytest.raises(DomainError) as exc_info:
        mcp_api.update_mcp_server(
            "missing",
            payload=UpdateMcpServerRequest(mcp_server_name="fs"),
            services=services,
        )

    assert exc_info.value.code == mcp_api.MCP_SERVER_NOT_FOUND
