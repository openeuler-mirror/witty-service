from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import Response

from witty_service.api.contract.dto.session import (
    CreateSessionRequest,
    SessionResponse,
    UpdateSessionRequest,
    UpdateSessionSelectionRequest,
)
from witty_service.api.contract.presenters import session_response
from witty_service.api.contract.routers._stub import not_implemented
from witty_service.api.errors import error_responses
from witty_service.application.session_service import SessionService

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _service(request: Request) -> SessionService:
    return cast(SessionService, request.app.state.services.session_service)


Service = Annotated[SessionService, Depends(_service)]


@router.post(
    "",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(400, 404, 409),
)
def create_session(payload: CreateSessionRequest, service: Service) -> SessionResponse:
    view = service.create_session(
        payload.workspace_id,
        title=payload.title,
        agent_id=payload.agent_id,
        model_id=payload.model_id,
    )
    return session_response(view)


@router.get(
    "/{session_id}", response_model=SessionResponse, responses=error_responses(404)
)
def get_session(session_id: str, service: Service) -> SessionResponse:
    return session_response(service.get_session(session_id))


@router.patch(
    "/{session_id}",
    response_model=SessionResponse,
    responses=error_responses(400, 404),
)
def update_session(
    session_id: str, payload: UpdateSessionRequest, service: Service
) -> SessionResponse:
    view = service.update_session(
        session_id,
        title=payload.title if "title" in payload.model_fields_set else None,
        pinned=payload.pinned if "pinned" in payload.model_fields_set else None,
    )
    return session_response(view)


@router.patch(
    "/{session_id}/selection",
    response_model=SessionResponse,
    responses=error_responses(400, 404, 409),
)
def update_session_selection(
    session_id: str, payload: UpdateSessionSelectionRequest, service: Service
) -> SessionResponse:
    view = service.update_selection(
        session_id, agent_id=payload.agent_id, model_id=payload.model_id
    )
    return session_response(view)


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(404, 409, 503),
)
def delete_session(session_id: str) -> Response:
    del session_id
    not_implemented()
