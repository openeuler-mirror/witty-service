from __future__ import annotations

from datetime import UTC, datetime

from witty_service.api.contract.dto.common import AgentRef, ModelRef
from witty_service.api.contract.dto.session import SessionResponse
from witty_service.api.contract.dto.turn import TurnSummary
from witty_service.application.session_service import SessionView
from witty_service.domain.records import AgentDefinitionRecord, ModelRecord, TurnRecord


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
