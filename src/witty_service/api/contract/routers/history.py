from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from fastapi.responses import Response

from witty_service.api.contract.dto.event import SessionEvent
from witty_service.api.contract.dto.message import MessageListResponse
from witty_service.api.contract.routers._stub import not_implemented
from witty_service.api.errors import error_responses

router = APIRouter()

_EVENT_STREAM_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {
        "description": (
            "SSE 事件流；每条为 `data: <SessionEvent JSON>`，不使用 `id:`/`event:`/"
            "`retry:`；客户端用 fetch + Bearer 读取"
        ),
        "content": {"text/event-stream": {"schema": SessionEvent.model_json_schema()}},
    },
    **error_responses(404, 503),
}


@router.get(
    "/sessions/{session_id}/messages",
    response_model=MessageListResponse,
    tags=["messages"],
    responses=error_responses(400, 404, 503),
)
def list_session_messages(
    session_id: str,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: str | None = None,
) -> MessageListResponse:
    not_implemented()


@router.get(
    "/sessions/{session_id}/events",
    response_class=Response,
    tags=["events"],
    responses=_EVENT_STREAM_RESPONSES,
)
def stream_session_events(session_id: str) -> Response:
    not_implemented()
