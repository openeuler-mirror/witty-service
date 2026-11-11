from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, status

from witty_service.api.contract.dto.session import SessionListResponse
from witty_service.api.contract.dto.workspace import (
    CreateWorkspaceRequest,
    UpdateWorkspaceRequest,
    WorkspaceListResponse,
    WorkspaceResponse,
)
from witty_service.api.contract.presenters import session_response
from witty_service.api.contract.routers._stub import not_implemented
from witty_service.api.errors import error_responses
from witty_service.application.session_service import SessionService

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.get("", response_model=WorkspaceListResponse, responses=error_responses(400))
def list_workspaces(
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> WorkspaceListResponse:
    not_implemented()


@router.post(
    "",
    response_model=WorkspaceResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(400, 404, 409),
)
def create_workspace(payload: CreateWorkspaceRequest) -> WorkspaceResponse:
    not_implemented()


@router.get(
    "/{workspace_id}",
    response_model=WorkspaceResponse,
    responses=error_responses(404),
)
def get_workspace(workspace_id: str) -> WorkspaceResponse:
    not_implemented()


@router.patch(
    "/{workspace_id}",
    response_model=WorkspaceResponse,
    responses=error_responses(400, 404, 409),
)
def update_workspace(
    workspace_id: str, payload: UpdateWorkspaceRequest
) -> WorkspaceResponse:
    not_implemented()


@router.get(
    "/{workspace_id}/sessions",
    response_model=SessionListResponse,
    responses=error_responses(400, 404),
)
def list_workspace_sessions(
    workspace_id: str,
    request: Request,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SessionListResponse:
    service: SessionService = request.app.state.services.session_service
    page = service.list_sessions(workspace_id, cursor=cursor, limit=limit)
    return SessionListResponse(
        items=[session_response(item) for item in page.items],
        next_cursor=page.next_cursor,
    )
