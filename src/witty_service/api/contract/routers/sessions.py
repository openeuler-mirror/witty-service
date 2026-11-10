from __future__ import annotations

from fastapi import APIRouter, status
from fastapi.responses import Response

from witty_service.api.contract.dto.session import (
    CreateSessionRequest,
    SessionResponse,
    UpdateSessionRequest,
    UpdateSessionSelectionRequest,
)
from witty_service.api.contract.routers._stub import not_implemented
from witty_service.api.errors import error_responses

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post(
    "",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(400, 404, 409),
)
def create_session(payload: CreateSessionRequest) -> SessionResponse:
    not_implemented()


@router.get(
    "/{session_id}", response_model=SessionResponse, responses=error_responses(404)
)
def get_session(session_id: str) -> SessionResponse:
    not_implemented()


@router.patch(
    "/{session_id}",
    response_model=SessionResponse,
    responses=error_responses(400, 404),
)
def update_session(session_id: str, payload: UpdateSessionRequest) -> SessionResponse:
    not_implemented()


@router.patch(
    "/{session_id}/selection",
    response_model=SessionResponse,
    responses=error_responses(400, 404, 409),
)
def update_session_selection(
    session_id: str, payload: UpdateSessionSelectionRequest
) -> SessionResponse:
    not_implemented()


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(404, 409, 503),
)
def delete_session(session_id: str) -> Response:
    not_implemented()
