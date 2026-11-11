from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from witty_service.api.contract.dto.common import AgentRef, ModelRef
from witty_service.api.contract.dto.event import SessionEvent
from witty_service.api.contract.dto.message import (
    FileMessagePart,
    MessageAttribution,
    MessageResponse,
    MessageRole,
    ReasoningMessagePart,
    TextMessagePart,
    ToolMessagePart,
)
from witty_service.api.contract.dto.session import SessionResponse
from witty_service.api.contract.dto.turn import (
    TurnAgentAttribution,
    TurnError,
    TurnModelAttribution,
    TurnResponse,
    TurnSummary,
)
from witty_service.application.history_service import (
    HistoryMessage,
    SessionEventView,
    SessionResyncView,
)
from witty_service.application.session_service import SessionView
from witty_service.domain.records import AgentDefinitionRecord, ModelRecord, TurnRecord
from witty_service.runtime.contracts import (
    RuntimeFilePart,
    RuntimeMessageKind,
    RuntimeReasoningPart,
    RuntimeTextPart,
    RuntimeToolPart,
)


def utc_datetime(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def agent_ref(agent: AgentDefinitionRecord) -> AgentRef:
    return AgentRef(
        id=agent.id,
        name=agent.name,
        kind=agent.kind.value,
        archived=agent.archived_at is not None,
    )


def model_ref(model: ModelRecord) -> ModelRef:
    return ModelRef(
        id=model.id,
        display_name=model.display_name,
        provider_id=model.provider_id,
        provider_model_id=model.provider_model_id,
        enabled=bool(model.enabled),
    )


def turn_summary(turn: TurnRecord) -> TurnSummary:
    return TurnSummary(id=turn.id, turn_seq=turn.turn_seq, status=turn.status.value)


def turn_response(turn: TurnRecord) -> TurnResponse:
    return TurnResponse(
        id=turn.id,
        session_id=turn.session_id,
        turn_seq=turn.turn_seq,
        status=turn.status.value,
        agent=TurnAgentAttribution(id=turn.agent_id, name=turn.agent_name_snapshot),
        model=TurnModelAttribution(
            id=turn.model_id, display_name=turn.model_name_snapshot
        ),
        usage=None,
        error=(
            TurnError(code=turn.error_code, message=turn.error_message or "")
            if turn.error_code
            else None
        ),
        started_at=utc_datetime(turn.started_at),
        finished_at=(
            utc_datetime(turn.finished_at) if turn.finished_at is not None else None
        ),
        created_at=utc_datetime(turn.created_at),
    )


def session_response(view: SessionView) -> SessionResponse:
    session = view.session
    return SessionResponse(
        id=session.id,
        workspace_id=session.workspace_id,
        title=session.title,
        pinned=bool(session.pinned),
        origin=session.origin.value,
        scheduled_task_id=session.scheduled_task_id,
        current_agent=agent_ref(view.agent),
        current_model=model_ref(view.model) if view.model else None,
        active_turn=turn_summary(view.active_turn) if view.active_turn else None,
        runtime_status=view.runtime_status,
        last_activity_at=utc_datetime(session.last_activity_at),
        created_at=utc_datetime(session.created_at),
        updated_at=utc_datetime(session.updated_at),
    )


def message_response(view: HistoryMessage) -> MessageResponse:
    message = view.message
    turn = view.turn
    role: MessageRole = (
        "user" if message.kind is RuntimeMessageKind.USER else "assistant"
    )
    return MessageResponse(
        id=message.message_id,
        role=role,
        attribution=(
            MessageAttribution(
                turn_id=turn.id,
                turn_seq=turn.turn_seq,
                agent=TurnAgentAttribution(
                    id=turn.agent_id, name=turn.agent_name_snapshot
                ),
                model=TurnModelAttribution(
                    id=turn.model_id, display_name=turn.model_name_snapshot
                ),
            )
            if turn is not None
            else None
        ),
        parts=[part for part in map(_message_part, message.parts) if part is not None],
        created_at=utc_datetime(message.created_at),
    )


def _message_part(part: Any) -> Any:
    if isinstance(part, RuntimeTextPart):
        return TextMessagePart(text=part.text)
    if isinstance(part, RuntimeReasoningPart):
        return ReasoningMessagePart(text=part.text)
    if isinstance(part, RuntimeToolPart):
        return ToolMessagePart(
            call_id=part.call_id,
            name=part.name,
            status=part.status.value,
            input=part.input,
            output=part.output,
            error=part.error,
        )
    if isinstance(part, RuntimeFilePart):
        return FileMessagePart(
            relative_path=part.relative_path,
            mime=part.mime,
            filename=part.filename,
        )
    return None


def session_event(view: SessionEventView) -> SessionEvent:
    return SessionEvent(
        event_id=view.event_id,
        type=view.type,
        session_id=view.session_id,
        turn_id=view.turn_id,
        message_id=view.message_id,
        occurred_at=utc_datetime(view.occurred_at),
        payload=view.payload,
    )


def session_resync(session_id: str, view: SessionResyncView) -> SessionEvent:
    return SessionEvent(
        event_id=f"resync_{view.reason}_{view.last_durable_seq}",
        type="resync_required",
        session_id=session_id,
        turn_id=None,
        message_id=None,
        occurred_at=utc_datetime(int(time.time() * 1000)),
        payload={
            "reason": view.reason,
            "last_durable_seq": view.last_durable_seq,
            "full_reload": True,
        },
    )
