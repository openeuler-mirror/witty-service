from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response, status

from witty_service.api.auth import require_bearer_auth
from witty_service.api.schemas import (
    CreateMcpServerRequest,
    McpServerResponse,
    UpdateMcpServerRequest,
)
from witty_service.api.services import ServiceContainer
from witty_service.application.mcp_runtime_config import McpRuntimeConfigResolver
from witty_service.domain.errors import DomainError
from witty_service.persistence.repositories import McpServerRecord

router = APIRouter(
    prefix="/mcp-servers",
    tags=["mcp-servers"],
    dependencies=[Depends(require_bearer_auth)],
)

MCP_SERVER_NOT_FOUND = "MCP_SERVER_NOT_FOUND"
MCP_SERVER_CONFIG_INVALID = "MCP_SERVER_CONFIG_INVALID"


def get_services(request: Request) -> ServiceContainer:
    return request.app.state.services


def _extract_server_name(config: dict[str, Any]) -> str:
    if not isinstance(config, dict) or len(config) == 0:
        raise DomainError(
            code=MCP_SERVER_CONFIG_INVALID,
            message="MCP server config must be a non-empty dictionary.",
        )
    return next(iter(config.keys()))


@router.post("", response_model=McpServerResponse, status_code=status.HTTP_201_CREATED)
def create_mcp_server(
    payload: CreateMcpServerRequest,
    services: ServiceContainer = Depends(get_services),
) -> McpServerResponse:
    mcp_server_name = _extract_server_name(payload.mcp_server_config)
    mcp_server_config = McpRuntimeConfigResolver(services).sanitize_for_storage(
        mcp_server_name, payload.mcp_server_config
    )
    server = services.repository.create_mcp_server(
        mcp_server_name=mcp_server_name,
        mcp_server_config=mcp_server_config,
    )
    return _to_mcp_server_response(server)


@router.get("", response_model=list[McpServerResponse])
def list_mcp_servers(
    services: ServiceContainer = Depends(get_services),
) -> list[McpServerResponse]:
    servers = services.repository.list_mcp_servers()
    return [_to_mcp_server_response(server) for server in servers]


@router.delete("/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mcp_server(
    server_id: str,
    services: ServiceContainer = Depends(get_services),
) -> Response:
    server = services.repository.get_mcp_server(server_id)
    if server is None:
        raise DomainError(
            code=MCP_SERVER_NOT_FOUND,
            message="MCP Server was not found.",
            details={"server_id": server_id},
        )
    services.repository.delete_mcp_server(server_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{server_id}", response_model=McpServerResponse)
def update_mcp_server(
    server_id: str,
    payload: UpdateMcpServerRequest,
    services: ServiceContainer = Depends(get_services),
) -> McpServerResponse:
    server = services.repository.get_mcp_server(server_id)
    if server is None:
        raise DomainError(
            code=MCP_SERVER_NOT_FOUND,
            message="MCP Server was not found.",
            details={"server_id": server_id},
        )

    mcp_server_name = payload.mcp_server_name
    if mcp_server_name is None and payload.mcp_server_config is not None:
        if (
            server.mcp_server_name == "cvekit_mcp"
            and "cvekit_mcp" not in payload.mcp_server_config
            and "command" in payload.mcp_server_config
        ):
            # 兼容历史扁平 cvekit 配置：其 command/args/env 不是 server name。
            mcp_server_name = server.mcp_server_name
        else:
            mcp_server_name = _extract_server_name(payload.mcp_server_config)

    updated_server = services.repository.update_mcp_server(
        server_id=server_id,
        mcp_server_name=mcp_server_name,
        mcp_server_config=(
            McpRuntimeConfigResolver(services).sanitize_for_storage(
                mcp_server_name or server.mcp_server_name,
                payload.mcp_server_config,
            )
            if payload.mcp_server_config is not None
            else None
        ),
    )
    return _to_mcp_server_response(updated_server)


def _to_mcp_server_response(server: McpServerRecord) -> McpServerResponse:
    return McpServerResponse(
        id=server.id,
        mcp_server_name=server.mcp_server_name,
        mcp_server_config=server.mcp_server_config,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )
