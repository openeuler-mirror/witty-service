from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from witty_service.domain.error_codes import ErrorCode
from witty_service.domain.errors import DomainError
from witty_service.domain.records import SessionRecord, TurnRecord
from witty_service.persistence.entities.base import utc_ms_now
from witty_service.persistence.repository.agent import AgentRepository
from witty_service.persistence.repository.catalog import CatalogRepository
from witty_service.persistence.repository.session import (
    AgentNotAvailableError,
    ModelNotAvailableError,
    SelectionChangedError,
    SessionAdmissionError,
    SessionRepository,
)


@dataclass(frozen=True, slots=True)
class TurnSelection:
    session: SessionRecord
    agent_id: str
    model_id: str


class TurnService:
    def __init__(
        self,
        *,
        sessions: SessionRepository,
        agents: AgentRepository,
        catalog: CatalogRepository,
        clock=utc_ms_now,
    ) -> None:
        self._sessions = sessions
        self._agents = agents
        self._catalog = catalog
        self._clock = clock

    def resolve_selection(self, session_id: str) -> TurnSelection:
        session = self._sessions.get(session_id)
        if session is None:
            raise _session_not_found(session_id)
        model_id = session.current_model_id
        if model_id is None:
            raise DomainError(
                code=ErrorCode.MODEL_REQUIRED,
                message="Session has no model selected.",
                status_code=400,
                details={"session_id": session_id},
            )
        agent = self._agents.get(session.current_agent_id)
        if agent is None:
            raise _agent_not_found(session.current_agent_id)
        model = self._catalog.get_model(model_id)
        if model is None:
            raise _model_not_found(model_id)
        if session.deleted_at is not None:
            raise _admission_closed(session_id)
        if agent.archived_at is not None:
            raise _agent_archived(agent.id)
        if not model.enabled:
            raise _model_disabled(model.id)
        return TurnSelection(session=session, agent_id=agent.id, model_id=model_id)

    def admit(self, session_id: str, *, turn_id: str | None = None) -> TurnRecord:
        try:
            return self._sessions.start_turn(
                session_id,
                None,
                None,
                self._clock(),
                turn_id=turn_id,
            )
        except SelectionChangedError as exc:
            raise DomainError(
                code=ErrorCode.INVALID_TURN_OPERATION,
                message="Session selection changed during admission.",
                status_code=409,
                details={"session_id": exc.session_id},
            ) from exc
        except SessionAdmissionError as exc:
            raise _admission_closed(exc.session_id) from exc
        except AgentNotAvailableError as exc:
            if exc.archived:
                raise _agent_archived(exc.agent_id) from exc
            raise _agent_not_found(exc.agent_id) from exc
        except ModelNotAvailableError as exc:
            if exc.disabled:
                raise _model_disabled(exc.model_id) from exc
            raise _model_not_found(exc.model_id) from exc
        except IntegrityError as exc:
            if turn_id is not None and self._sessions.get_turn(turn_id) is not None:
                raise
            active = self._sessions.get_running_turn(session_id)
            if active is None:
                raise
            raise DomainError(
                code=ErrorCode.ACTIVE_TURN_CONFLICT,
                message="Session already has a running turn.",
                status_code=409,
                details={
                    "session_id": session_id,
                    "active_turn_id": active.id,
                },
            ) from exc

    def start_turn(
        self, session_id: str, *, turn_id: str | None = None
    ) -> TurnRecord:
        self.resolve_selection(session_id)
        return self.admit(session_id, turn_id=turn_id)


def _session_not_found(session_id: str) -> DomainError:
    return DomainError(
        code=ErrorCode.SESSION_NOT_FOUND,
        message="Session was not found.",
        status_code=404,
        details={"session_id": session_id},
    )


def _admission_closed(session_id: str) -> DomainError:
    return DomainError(
        code=ErrorCode.SESSION_ADMISSION_CLOSED,
        message="Session is closed for new turns.",
        status_code=409,
        details={"session_id": session_id},
    )


def _agent_not_found(agent_id: str) -> DomainError:
    return DomainError(
        code=ErrorCode.AGENT_NOT_FOUND,
        message="Agent was not found.",
        status_code=404,
        details={"agent_id": agent_id},
    )


def _agent_archived(agent_id: str) -> DomainError:
    return DomainError(
        code=ErrorCode.AGENT_ARCHIVED,
        message="Agent is archived.",
        status_code=409,
        details={"agent_id": agent_id},
    )


def _model_not_found(model_id: str) -> DomainError:
    return DomainError(
        code=ErrorCode.MODEL_NOT_FOUND,
        message="Model was not found.",
        status_code=404,
        details={"model_id": model_id},
    )


def _model_disabled(model_id: str) -> DomainError:
    return DomainError(
        code=ErrorCode.MODEL_DISABLED,
        message="Model is disabled.",
        status_code=409,
        details={"model_id": model_id},
    )
