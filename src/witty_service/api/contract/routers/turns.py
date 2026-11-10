from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from witty_service.api.contract.dto.turn import (
    AbortTurnResponse,
    CreateTurnRequest,
    TurnListResponse,
    TurnResponse,
)
from witty_service.api.contract.routers._stub import not_implemented
from witty_service.api.errors import error_responses

router = APIRouter(tags=["turns"])


@router.post(
    "/sessions/{session_id}/turns",
    response_model=TurnResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(400, 404, 409, 503),
)
def create_turn(session_id: str, payload: CreateTurnRequest) -> TurnResponse:
    not_implemented()


@router.get(
    "/sessions/{session_id}/turns",
    response_model=TurnListResponse,
    responses=error_responses(400, 404),
)
def list_session_turns(
    session_id: str,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TurnListResponse:
    not_implemented()


@router.post(
    "/sessions/{session_id}/turns/{turn_id}/abort",
    response_model=AbortTurnResponse,
    responses=error_responses(404, 503),
)
def abort_turn(session_id: str, turn_id: str) -> AbortTurnResponse:
    not_implemented()


@router.post(
    "/sessions/{session_id}/turns/{turn_id}/regenerate",
    response_model=TurnResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(400, 404, 409, 503),
)
def regenerate_turn(session_id: str, turn_id: str) -> TurnResponse:
    not_implemented()
