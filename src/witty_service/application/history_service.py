from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Any, cast

from witty_service.application.pagination import decode_cursor, encode_cursor
from witty_service.domain.error_codes import ErrorCode
from witty_service.domain.errors import DomainError
from witty_service.domain.records import SessionRecord, TurnRecord
from witty_service.persistence.repository.session import SessionRepository
from witty_service.runtime.contracts import (
    HistoryQuery,
    RuntimeEventEnvelope,
    RuntimeEventType,
    RuntimeHistoryMessage,
    RuntimeMessageKind,
)
from witty_service.runtime.events import (
    RuntimeResyncRequired,
    RuntimeStreamItem,
    RuntimeTurnEvent,
)
from witty_service.runtime.manager import WorkspaceRuntimeManager

_MESSAGE_KINDS = frozenset({RuntimeMessageKind.USER, RuntimeMessageKind.ASSISTANT})

_EVENT_TYPES = {
    RuntimeEventType.MESSAGE_DELTA: "message.delta",
    RuntimeEventType.MESSAGE_COMPLETED: "message.completed",
    RuntimeEventType.THINKING_DELTA: "thinking.delta",
    RuntimeEventType.TOOL_CALL_STARTED: "tool.call.started",
    RuntimeEventType.TOOL_CALL_DELTA: "tool.call.delta",
    RuntimeEventType.TOOL_CALL_RESPONSE: "tool.call.response",
    RuntimeEventType.ARTIFACT_STARTED: "artifact.started",
    RuntimeEventType.ARTIFACT_DELTA: "artifact.delta",
    RuntimeEventType.ARTIFACT_COMPLETED: "artifact.completed",
    RuntimeEventType.SESSION_USAGE: "session.usage",
    RuntimeEventType.TURN_STARTED: "turn.updated",
    RuntimeEventType.TURN_COMPLETED: "turn.updated",
    RuntimeEventType.TURN_FAILED: "turn.updated",
    RuntimeEventType.SESSION_ERROR: "runtime.status",
}

_TURN_STATUS = {
    RuntimeEventType.TURN_STARTED: "running",
    RuntimeEventType.TURN_COMPLETED: "completed",
    RuntimeEventType.TURN_FAILED: "error",
    RuntimeEventType.SESSION_ERROR: "error",
}


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    message: RuntimeHistoryMessage
    turn: TurnRecord | None


@dataclass(frozen=True, slots=True)
class MessagePage:
    items: tuple[HistoryMessage, ...]
    next_cursor: str | None
    previous_cursor: str | None


@dataclass(frozen=True, slots=True)
class SessionEventView:
    event_id: str
    type: str
    session_id: str
    turn_id: str | None
    message_id: str | None
    occurred_at: int
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SessionResyncView:
    reason: str
    last_durable_seq: int


EventStreamItem = SessionEventView | SessionResyncView


class HistoryService:
    def __init__(
        self, *, sessions: SessionRepository, runtime: WorkspaceRuntimeManager
    ) -> None:
        self._sessions = sessions
        self._runtime = runtime

    async def list_messages(
        self, session_id: str, *, limit: int, cursor: str | None
    ) -> MessagePage:
        runtime_cursor = self._decode_cursor(session_id, cursor)
        session = self._require_session(session_id)
        if session.runtime_session_id is None:
            return MessagePage(items=(), next_cursor=None, previous_cursor=None)
        page = await self._runtime.read_history(
            HistoryQuery(
                workspace_id=session.workspace_id,
                runtime_session_id=session.runtime_session_id,
                limit=limit,
                cursor=runtime_cursor,
            )
        )
        messages = tuple(
            message for message in page.items if message.kind in _MESSAGE_KINDS
        )
        turns = self._sessions.runtime_message_turns(
            session_id, [message.message_id for message in messages]
        )
        items = tuple(
            HistoryMessage(message=message, turn=turns.get(message.message_id))
            for message in messages
        )
        return MessagePage(
            items=items,
            next_cursor=self._encode_cursor(session_id, page.next_cursor),
            previous_cursor=self._encode_cursor(session_id, page.previous_cursor),
        )

    async def stream_events(self, session_id: str) -> AsyncIterator[EventStreamItem]:
        session = self._require_session(session_id)
        return self._iter_events(session)

    async def _iter_events(
        self, session: SessionRecord
    ) -> AsyncIterator[EventStreamItem]:
        turn_sessions: dict[str, str] = {}
        runtime_session_id = session.runtime_session_id
        stream = cast(
            AsyncGenerator[RuntimeStreamItem, None],
            self._runtime.subscribe_events(session.workspace_id),
        )
        async with contextlib.aclosing(stream):
            async for item in stream:
                if isinstance(item, RuntimeResyncRequired):
                    yield SessionResyncView(
                        reason=item.reason.value,
                        last_durable_seq=item.last_durable_seq,
                    )
                    continue
                if not isinstance(item, RuntimeTurnEvent):
                    continue
                if item.turn_id is None:
                    if item.event.runtime_session_id != runtime_session_id:
                        current = self._sessions.get(session.id)
                        runtime_session_id = (
                            current.runtime_session_id if current is not None else None
                        )
                    if (
                        runtime_session_id is not None
                        and item.event.runtime_session_id == runtime_session_id
                    ):
                        event = _session_event(session.id, item)
                        if event is not None:
                            yield event
                    continue
                if not self._turn_belongs_to(session.id, item.turn_id, turn_sessions):
                    continue
                event = _session_event(session.id, item)
                if event is not None:
                    yield event

    def _turn_belongs_to(
        self, session_id: str, turn_id: str, cache: dict[str, str]
    ) -> bool:
        owner = cache.get(turn_id)
        if owner is None:
            turn = self._sessions.get_turn(turn_id)
            if turn is None:
                return False
            owner = turn.session_id
            cache[turn_id] = owner
        return owner == session_id

    def _require_session(self, session_id: str) -> SessionRecord:
        session = self._sessions.get(session_id)
        if session is None or session.deleted_at is not None:
            raise _session_not_found(session_id)
        return session

    def _decode_cursor(self, session_id: str, cursor: str | None) -> str | None:
        if cursor is None:
            return None
        position = decode_cursor(
            cursor,
            scope="message",
            scope_id=session_id,
            fields={"runtime_cursor": str},
            mismatch_code=ErrorCode.CURSOR_SESSION_MISMATCH,
        )
        return str(position["runtime_cursor"])

    def _encode_cursor(self, session_id: str, runtime_cursor: str | None) -> str | None:
        if runtime_cursor is None:
            return None
        return encode_cursor(
            scope="message",
            scope_id=session_id,
            position={"runtime_cursor": runtime_cursor},
        )


def _session_event(session_id: str, item: RuntimeTurnEvent) -> SessionEventView | None:
    event = item.event
    event_type = _EVENT_TYPES.get(event.type)
    if event_type is None:
        return None
    return SessionEventView(
        event_id=event.event_id,
        type=event_type,
        session_id=session_id,
        turn_id=item.turn_id,
        message_id=event.runtime_message_id,
        occurred_at=event.occurred_at,
        payload=_event_payload(event),
    )


def _event_payload(event: RuntimeEventEnvelope) -> dict[str, Any]:
    payload = event.payload.to_dict()
    status = _TURN_STATUS.get(event.type)
    if status is not None:
        payload["status"] = status
    return payload


def _session_not_found(session_id: str) -> DomainError:
    return DomainError(
        code=ErrorCode.SESSION_NOT_FOUND,
        message="Session was not found.",
        status_code=404,
        details={"session_id": session_id},
    )
