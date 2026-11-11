from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response, StreamingResponse

from witty_service.api.contract.dto.event import SessionEvent
from witty_service.api.contract.dto.message import MessageListResponse
from witty_service.api.contract.presenters import (
    message_response,
    session_event,
    session_resync,
)
from witty_service.api.errors import error_responses
from witty_service.application.history_service import (
    EventStreamItem,
    HistoryService,
    SessionResyncView,
)

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


def _service(request: Request) -> HistoryService:
    return cast(HistoryService, request.app.state.services.history_service)


Service = Annotated[HistoryService, Depends(_service)]


@router.get(
    "/sessions/{session_id}/messages",
    response_model=MessageListResponse,
    tags=["messages"],
    responses=error_responses(400, 404, 503),
)
async def list_session_messages(
    session_id: str,
    service: Service,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> MessageListResponse:
    page = await service.list_messages(session_id, limit=limit, cursor=cursor)
    return MessageListResponse(
        items=[message_response(item) for item in page.items],
        next_cursor=page.next_cursor,
        previous_cursor=page.previous_cursor,
    )


@router.get(
    "/sessions/{session_id}/events",
    response_class=Response,
    tags=["events"],
    responses=_EVENT_STREAM_RESPONSES,
)
async def stream_session_events(session_id: str, service: Service) -> Response:
    events = await service.stream_events(session_id)
    return StreamingResponse(
        _event_frames(session_id, events),
        media_type="text/event-stream",
    )


async def _event_frames(
    session_id: str, events: AsyncIterator[EventStreamItem]
) -> AsyncIterator[str]:
    stream = cast(AsyncGenerator[EventStreamItem, None], events)
    async with contextlib.aclosing(stream):
        async for item in stream:
            event = (
                session_resync(session_id, item)
                if isinstance(item, SessionResyncView)
                else session_event(item)
            )
            yield f"data: {event.model_dump_json()}\n\n"
