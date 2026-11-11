from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request, status

from witty_service.api.contract.dto.turn import (
    AbortTurnResponse,
    CreateTurnRequest,
    TextInputPart,
    TurnListResponse,
    TurnResponse,
)
from witty_service.api.contract.presenters import turn_response
from witty_service.api.contract.routers._stub import not_implemented
from witty_service.api.errors import error_responses
from witty_service.application.turn_service import TurnService
from witty_service.runtime.contracts import (
    RuntimeFilePart,
    RuntimeInputPart,
    RuntimeTextPart,
)

router = APIRouter(tags=["turns"])


def _service(request: Request) -> TurnService:
    return cast(TurnService, request.app.state.services.turn_service)


Service = Annotated[TurnService, Depends(_service)]


@router.post(
    "/sessions/{session_id}/turns",
    response_model=TurnResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(400, 404, 409, 503),
)
async def create_turn(
    session_id: str, payload: CreateTurnRequest, service: Service
) -> TurnResponse:
    parts: list[RuntimeInputPart] = []
    for part in payload.parts:
        if isinstance(part, TextInputPart):
            parts.append(RuntimeTextPart(text=part.text))
        else:
            parts.append(
                RuntimeFilePart(
                    relative_path=part.relative_path,
                    mime=part.mime,
                    filename=part.filename,
                )
            )
    turn = await service.execute_turn(session_id, parts=tuple(parts))
    return turn_response(turn)


@router.get(
    "/sessions/{session_id}/turns",
    response_model=TurnListResponse,
    responses=error_responses(400, 404),
)
def list_session_turns(
    session_id: str,
    service: Service,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TurnListResponse:
    page = service.list_turns(session_id, cursor=cursor, limit=limit)
    return TurnListResponse(
        items=[turn_response(turn) for turn in page.items],
        next_cursor=page.next_cursor,
    )


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
