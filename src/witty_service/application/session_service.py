from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from witty_service.application.pagination import decode_cursor, encode_cursor
from witty_service.domain.enums import SessionOrigin
from witty_service.domain.error_codes import ErrorCode
from witty_service.domain.errors import DomainError
from witty_service.domain.records import (
    AgentDefinitionRecord,
    ModelRecord,
    SessionRecord,
    TurnRecord,
    WorkspaceRecord,
)
from witty_service.persistence.entities.base import utc_ms_now
from witty_service.persistence.repository.agent import AgentRepository
from witty_service.persistence.repository.catalog import CatalogRepository
from witty_service.persistence.repository.session import SessionRepository
from witty_service.persistence.repository.workspace import WorkspaceRepository
from witty_service.runtime.manager import RuntimeStatus, WorkspaceRuntimeManager

_SESSION_CURSOR_SCOPE = "session"


@dataclass(frozen=True, slots=True)
class SessionView:
    session: SessionRecord
    agent: AgentDefinitionRecord
    model: ModelRecord | None
    active_turn: TurnRecord | None
    runtime_status: RuntimeStatus


@dataclass(frozen=True, slots=True)
class SessionPage:
    items: tuple[SessionView, ...]
    next_cursor: str | None


class SessionService:
    def __init__(
        self,
        *,
        sessions: SessionRepository,
        workspaces: WorkspaceRepository,
        agents: AgentRepository,
        catalog: CatalogRepository,
        runtime_manager: WorkspaceRuntimeManager,
        clock: Callable[[], int] = utc_ms_now,
    ) -> None:
        self._sessions = sessions
        self._workspaces = workspaces
        self._agents = agents
        self._catalog = catalog
        self._runtime_manager = runtime_manager
        self._clock = clock

    def create_session(
        self,
        workspace_id: str,
        *,
        title: str | None,
        agent_id: str | None,
        model_id: str | None,
    ) -> SessionView:
        workspace = self._require_workspace(workspace_id)
        agent = self._load_agent(
            agent_id if agent_id is not None else workspace.default_agent_id
        )
        model = None
        if model_id is not None:
            model = self._catalog.get_model(model_id)
            if model is None:
                raise _model_not_found(model_id)
        elif workspace.default_model_id is not None:
            model = self._catalog.get_model(workspace.default_model_id)
        if agent.archived_at is not None:
            raise DomainError(
                code=ErrorCode.AGENT_ARCHIVED,
                message="Agent is archived.",
                status_code=409,
                details={"agent_id": agent.id},
            )
        if model_id is not None and model is not None and not model.enabled:
            raise DomainError(
                code=ErrorCode.MODEL_DISABLED,
                message="Model is disabled.",
                status_code=409,
                details={"model_id": model.id},
            )

        session = self._sessions.create(
            workspace_id,
            agent.id,
            model.id if model else None,
            SessionOrigin.web,
            title,
            None,
            None,
            self._clock(),
        )
        return self._view(session, agent, model, None)

    def get_session(self, session_id: str) -> SessionView:
        session = self._require_session(session_id)
        return self._view_for_session(session)

    def list_sessions(
        self, workspace_id: str, *, cursor: str | None, limit: int
    ) -> SessionPage:
        after: tuple[int, int, str] | None = None
        if cursor is not None:
            position = decode_cursor(
                cursor,
                scope=_SESSION_CURSOR_SCOPE,
                scope_id=workspace_id,
                fields={
                    "pinned": int,
                    "last_activity_at": int,
                    "session_id": str,
                },
            )
            after = (
                position["pinned"],
                position["last_activity_at"],
                position["session_id"],
            )
        self._require_workspace(workspace_id)
        rows = self._sessions.list_by_workspace_page(workspace_id, after, limit + 1)
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more:
            last = page[-1]
            next_cursor = encode_cursor(
                scope=_SESSION_CURSOR_SCOPE,
                scope_id=workspace_id,
                position={
                    "pinned": last.pinned,
                    "last_activity_at": last.last_activity_at,
                    "session_id": last.id,
                },
            )
        return SessionPage(
            items=tuple(self._view_for_session(row) for row in page),
            next_cursor=next_cursor,
        )

    def update_session(
        self,
        session_id: str,
        *,
        title: str | None,
        pinned: bool | None,
    ) -> SessionView:
        session = self._require_session(session_id)
        updated = self._sessions.update_metadata(
            session.id,
            title,
            None if pinned is None else int(pinned),
            self._clock(),
        )
        if updated is None:
            raise _session_not_found(session_id)
        return self._view_for_session(updated)

    def update_selection(
        self, session_id: str, *, agent_id: str | None, model_id: str | None
    ) -> SessionView:
        session = self._require_session(session_id)
        agent = None
        model = None
        if agent_id is not None:
            agent = self._load_agent(agent_id)
        if model_id is not None:
            model = self._catalog.get_model(model_id)
            if model is None:
                raise _model_not_found(model_id)
        if agent is not None and agent.archived_at is not None:
            raise DomainError(
                code=ErrorCode.AGENT_ARCHIVED,
                message="Agent is archived.",
                status_code=409,
                details={"agent_id": agent.id},
            )
        if model is not None and not model.enabled:
            raise DomainError(
                code=ErrorCode.MODEL_DISABLED,
                message="Model is disabled.",
                status_code=409,
                details={"model_id": model.id},
            )
        updated = self._sessions.update_current_settings(
            session.id,
            agent.id if agent is not None else None,
            model.id if model is not None else None,
            self._clock(),
        )
        if updated is None:
            raise _session_not_found(session_id)
        return self._view_for_session(updated)

    def _view_for_session(self, session: SessionRecord) -> SessionView:
        agent = self._load_agent(session.current_agent_id)
        model = (
            self._catalog.get_model(session.current_model_id)
            if session.current_model_id is not None
            else None
        )
        return self._view(
            session,
            agent,
            model,
            self._sessions.get_running_turn(session.id),
        )

    def _view(
        self,
        session: SessionRecord,
        agent: AgentDefinitionRecord,
        model: ModelRecord | None,
        active_turn: TurnRecord | None,
    ) -> SessionView:
        return SessionView(
            session=session,
            agent=agent,
            model=model,
            active_turn=active_turn,
            runtime_status=_runtime_status(self._runtime_manager, session.workspace_id),
        )

    def _require_workspace(self, workspace_id: str) -> WorkspaceRecord:
        workspace = self._workspaces.get(workspace_id)
        if workspace is None:
            raise DomainError(
                code=ErrorCode.WORKSPACE_NOT_FOUND,
                message="Workspace was not found.",
                status_code=404,
                details={"workspace_id": workspace_id},
            )
        return workspace

    def _require_session(self, session_id: str) -> SessionRecord:
        session = self._sessions.get(session_id)
        if session is None or session.deleted_at is not None:
            raise _session_not_found(session_id)
        return session

    def _require_agent(self, agent_id: str) -> AgentDefinitionRecord:
        agent = self._load_agent(agent_id)
        if agent.archived_at is not None:
            raise DomainError(
                code=ErrorCode.AGENT_ARCHIVED,
                message="Agent is archived.",
                status_code=409,
                details={"agent_id": agent_id},
            )
        return agent

    def _load_agent(self, agent_id: str) -> AgentDefinitionRecord:
        agent = self._agents.get(agent_id)
        if agent is None:
            raise _agent_not_found(agent_id)
        return agent

    def _require_enabled_model(self, model_id: str) -> ModelRecord:
        model = self._catalog.get_model(model_id)
        if model is None:
            raise _model_not_found(model_id)
        if not model.enabled:
            raise DomainError(
                code=ErrorCode.MODEL_DISABLED,
                message="Model is disabled.",
                status_code=409,
                details={"model_id": model_id},
            )
        return model


def _runtime_status(
    runtime_manager: WorkspaceRuntimeManager, workspace_id: str
) -> RuntimeStatus:
    return runtime_manager.availability(workspace_id).status


def _session_not_found(session_id: str) -> DomainError:
    return DomainError(
        code=ErrorCode.SESSION_NOT_FOUND,
        message="Session was not found.",
        status_code=404,
        details={"session_id": session_id},
    )


def _agent_not_found(agent_id: str) -> DomainError:
    return DomainError(
        code=ErrorCode.AGENT_NOT_FOUND,
        message="Agent was not found.",
        status_code=404,
        details={"agent_id": agent_id},
    )


def _model_not_found(model_id: str) -> DomainError:
    return DomainError(
        code=ErrorCode.MODEL_NOT_FOUND,
        message="Model was not found.",
        status_code=404,
        details={"model_id": model_id},
    )
