from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import cast
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from witty_service.application.pagination import decode_cursor, encode_cursor
from witty_service.domain.enums import TurnStatus
from witty_service.domain.error_codes import ErrorCode
from witty_service.domain.errors import DomainError
from witty_service.domain.records import (
    SessionRecord,
    TurnRecord,
    WorkspaceRecord,
)
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
from witty_service.persistence.repository.workspace import WorkspaceRepository
from witty_service.runtime.contracts import (
    RuntimeErrorPayload,
    RuntimeEventType,
    RuntimeFilePart,
    RuntimeInputPart,
    RuntimeModelRef,
    TurnExecutionRequest,
    TurnPermit,
)
from witty_service.runtime.events import (
    RuntimeResyncReason,
    RuntimeResyncRequired,
    RuntimeStreamItem,
    RuntimeTurnEvent,
)
from witty_service.runtime.manager import (
    RuntimePhase,
    WorkspaceRuntimeManager,
    WorkspaceRuntimeSpec,
)
from witty_service.runtime.projection import agent_internal_name

logger = logging.getLogger(__name__)

_PUMP_RECONNECT_DELAY = 0.5


@dataclass(frozen=True, slots=True)
class TurnSelection:
    session: SessionRecord
    agent_id: str
    model_id: str


@dataclass(frozen=True, slots=True)
class TurnPage:
    items: tuple[TurnRecord, ...]
    next_cursor: str | None


class TurnService:
    def __init__(
        self,
        *,
        sessions: SessionRepository,
        workspaces: WorkspaceRepository,
        agents: AgentRepository,
        catalog: CatalogRepository,
        runtime: WorkspaceRuntimeManager,
        runtime_spec_factory: Callable[[WorkspaceRecord], WorkspaceRuntimeSpec],
        clock: Callable[[], int] = utc_ms_now,
    ) -> None:
        self._sessions = sessions
        self._workspaces = workspaces
        self._agents = agents
        self._catalog = catalog
        self._runtime = runtime
        self._runtime_spec_factory = runtime_spec_factory
        self._clock = clock
        self._permits: dict[str, TurnPermit] = {}
        self._pumps: dict[str, asyncio.Task[None]] = {}
        self._pump_ready: dict[str, asyncio.Event] = {}
        self._closing = False

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

    def start_turn(self, session_id: str, *, turn_id: str | None = None) -> TurnRecord:
        self.resolve_selection(session_id)
        return self.admit(session_id, turn_id=turn_id)

    async def execute_turn(
        self, session_id: str, *, parts: tuple[RuntimeInputPart, ...]
    ) -> TurnRecord:
        """按固定顺序准入并提交 v2 admission；admitted 不等于 started。"""
        self._raise_if_closing()
        selection = self.resolve_selection(session_id)
        if any(isinstance(part, RuntimeFilePart) for part in parts):
            raise DomainError(
                code=ErrorCode.VALIDATION_ERROR,
                message="File parts are not supported.",
                status_code=400,
                details={"field": "parts"},
            )
        workspace = self._workspaces.get(selection.session.workspace_id)
        if workspace is None:
            raise _workspace_not_found(selection.session.workspace_id)
        spec = self._runtime_spec_factory(workspace)
        await self._runtime.ensure_started(spec)
        self._raise_if_closing()
        await self._ensure_pump(workspace.id)
        self._raise_if_closing()

        turn_id = uuid4().hex
        permit = await self._runtime.acquire_turn_permit(
            spec,
            turn_id=turn_id,
            runtime_session_id=selection.session.runtime_session_id,
        )
        self._permits[turn_id] = permit
        turn: TurnRecord | None = None
        admitted = False
        try:
            self._raise_if_closing()
            turn = self.admit(session_id, turn_id=turn_id)
            runtime_session = await self._runtime.ensure_runtime_session(
                workspace_id=workspace.id,
                session_id=session_id,
                runtime_session_id=selection.session.runtime_session_id,
            )
            self._raise_if_closing()
            persisted = self._sessions.set_runtime_session_id(
                session_id, runtime_session.runtime_session_id, self._clock()
            )
            if persisted is None:
                raise _session_not_found(session_id)
            await self._runtime.bind_turn_runtime_session(
                workspace_id=workspace.id,
                turn_id=turn_id,
                runtime_session_id=runtime_session.runtime_session_id,
            )
            self._raise_if_closing()
            await self._runtime.admit_turn(
                self._execution_request(
                    workspace=workspace,
                    session_id=session_id,
                    turn=turn,
                    runtime_session_id=runtime_session.runtime_session_id,
                    parts=parts,
                )
            )
            admitted = True
            return self._sessions.get_turn(turn_id) or turn
        except BaseException as exc:
            if turn is not None:
                self._sessions.finish_turn(
                    turn.id,
                    TurnStatus.error,
                    _error_code(exc),
                    _error_message(exc),
                    self._clock(),
                )
            raise
        finally:
            if not admitted:
                held = self._permits.pop(turn_id, None)
                if held is not None:
                    await self._runtime.release_turn_permit(held)

    async def close(self) -> None:
        self._closing = True
        for ready in self._pump_ready.values():
            ready.set()
        pumps = tuple(self._pumps.values())
        self._pumps.clear()
        for task in pumps:
            task.cancel()
        if pumps:
            await asyncio.gather(*pumps, return_exceptions=True)
        self._pump_ready.clear()
        for turn_id in tuple(self._permits):
            await self._interrupt_turn(turn_id, "SERVICE_SHUTDOWN")

    async def _interrupt_turn(self, turn_id: str, error_code: str) -> None:
        permit = self._permits.pop(turn_id, None)
        if permit is None:
            return
        try:
            self._sessions.finish_turn(
                turn_id,
                TurnStatus.interrupted,
                error_code,
                "runtime execution was interrupted",
                self._clock(),
            )
        except Exception:
            logger.exception("Failed to interrupt turn %s", turn_id)
        finally:
            try:
                await self._runtime.release_turn_permit(permit)
            except Exception:
                logger.exception("Failed to release turn permit %s", permit.permit_id)

    def _raise_if_closing(self) -> None:
        if self._closing:
            raise DomainError(
                code=ErrorCode.RUNTIME_UNAVAILABLE,
                message="Turn execution is shutting down.",
                status_code=503,
            )

    def _execution_request(
        self,
        *,
        workspace: WorkspaceRecord,
        session_id: str,
        turn: TurnRecord,
        runtime_session_id: str,
        parts: tuple[RuntimeInputPart, ...],
    ) -> TurnExecutionRequest:
        agent = self._agents.get(turn.agent_id)
        if agent is None:
            raise _agent_not_found(turn.agent_id)
        model = self._catalog.get_model(turn.model_id)
        if model is None:
            raise _model_not_found(turn.model_id)
        return TurnExecutionRequest(
            workspace_id=workspace.id,
            session_id=session_id,
            turn_id=turn.id,
            runtime_session_id=runtime_session_id,
            message_id=f"msg_{turn.id}",
            agent_internal_name=agent_internal_name(agent.id),
            model=RuntimeModelRef(
                provider_id=model.provider_id, model_id=model.provider_model_id
            ),
            parts=parts,
        )

    async def _ensure_pump(self, workspace_id: str) -> None:
        while True:
            task = self._pumps.get(workspace_id)
            if task is None or task.done():
                ready = asyncio.Event()
                self._pump_ready[workspace_id] = ready
                task = asyncio.create_task(self._consume_events(workspace_id, ready))
                self._pumps[workspace_id] = task
                await ready.wait()
                self._raise_if_closing()
                if task.done():
                    raise _runtime_unavailable(workspace_id)
                return
            current_ready = self._pump_ready.get(workspace_id)
            if current_ready is not None:
                await current_ready.wait()
                self._raise_if_closing()
                if task.done():
                    raise _runtime_unavailable(workspace_id)
                return
            await asyncio.sleep(0)

    async def _consume_events(self, workspace_id: str, ready: asyncio.Event) -> None:
        task = asyncio.current_task()
        try:
            while True:
                ready.clear()
                stream = cast(
                    AsyncGenerator[RuntimeStreamItem, None],
                    self._runtime.subscribe_events(workspace_id),
                )
                async with contextlib.aclosing(stream):
                    ready.set()
                    async for item in stream:
                        if isinstance(item, RuntimeResyncRequired):
                            ready.clear()
                            await self._handle_resync(workspace_id, item)
                            if item.reason in (
                                RuntimeResyncReason.RUNTIME_STOPPED,
                                RuntimeResyncReason.RUNTIME_EXITED,
                            ):
                                return
                            if item.reason is RuntimeResyncReason.SUBSCRIBER_LAGGED:
                                break
                            ready.set()
                            continue
                        if (
                            isinstance(item, RuntimeTurnEvent)
                            and item.turn_id is not None
                        ):
                            try:
                                await self._apply_event(item)
                            except Exception:
                                logger.exception("Failed to apply turn event")
                                await self._interrupt_turn(
                                    item.turn_id, "RUNTIME_EVENT_ERROR"
                                )
                ready.clear()
                if self._pumps.get(workspace_id) is not task:
                    return
                if (
                    self._runtime.get_state(workspace_id).phase
                    is not RuntimePhase.HEALTHY
                ):
                    return
                await asyncio.sleep(_PUMP_RECONNECT_DELAY)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Turn event pump stopped for workspace %s", workspace_id)
        finally:
            ready.set()
            if self._pump_ready.get(workspace_id) is ready:
                self._pump_ready.pop(workspace_id, None)
            if self._pumps.get(workspace_id) is task:
                self._pumps.pop(workspace_id, None)

    async def _handle_resync(
        self, workspace_id: str, item: RuntimeResyncRequired
    ) -> None:
        if item.reason in (
            RuntimeResyncReason.RUNTIME_RESTARTED,
            RuntimeResyncReason.RUNTIME_EXITED,
            RuntimeResyncReason.SUBSCRIBER_LAGGED,
            RuntimeResyncReason.RUNTIME_STOPPED,
        ):
            for turn_id in tuple(self._permits):
                permit = self._permits.get(turn_id)
                if permit is not None and permit.workspace_id == workspace_id:
                    await self._interrupt_turn(turn_id, "RUNTIME_RESYNC_REQUIRED")

    async def _apply_event(self, item: RuntimeTurnEvent) -> None:
        turn_id = item.turn_id
        if turn_id is None:
            return
        event_type = item.event.type
        if event_type is RuntimeEventType.TURN_COMPLETED:
            status = TurnStatus.completed
            error_code = None
            error_message = None
        elif event_type in (
            RuntimeEventType.TURN_FAILED,
            RuntimeEventType.SESSION_ERROR,
        ):
            status = TurnStatus.error
            payload = item.event.payload
            if isinstance(payload, RuntimeErrorPayload):
                error_code = payload.code
                error_message = payload.message
            else:
                error_code = "TURN_FAILED"
                error_message = "runtime reported a failed turn"
        else:
            return
        self._sessions.finish_turn(
            turn_id,
            status,
            error_code,
            error_message,
            self._clock(),
        )
        permit = self._permits.pop(turn_id, None)
        if permit is not None:
            await self._runtime.release_turn_permit(permit)

    def get_turn(self, session_id: str, turn_id: str) -> TurnRecord:
        self._require_session(session_id)
        turn = self._sessions.get_turn(turn_id)
        if turn is None or turn.session_id != session_id:
            raise DomainError(
                code=ErrorCode.TURN_NOT_FOUND,
                message="Turn was not found.",
                status_code=404,
                details={"session_id": session_id, "turn_id": turn_id},
            )
        return turn

    def list_turns(
        self, session_id: str, *, cursor: str | None, limit: int
    ) -> TurnPage:
        before_seq: int | None = None
        if cursor is not None:
            position = decode_cursor(
                cursor,
                scope="turn",
                scope_id=session_id,
                fields={"turn_seq": int},
            )
            before_seq = position["turn_seq"]
            if before_seq < 1:
                raise DomainError(
                    code=ErrorCode.INVALID_CURSOR,
                    message="cursor is invalid",
                    status_code=400,
                )
        self._require_session(session_id)
        rows = self._sessions.list_turns_page(session_id, before_seq, limit + 1)
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more:
            next_cursor = encode_cursor(
                scope="turn",
                scope_id=session_id,
                position={"turn_seq": page[-1].turn_seq},
            )
        return TurnPage(items=tuple(page), next_cursor=next_cursor)

    def _require_session(self, session_id: str) -> SessionRecord:
        session = self._sessions.get(session_id)
        if session is None or session.deleted_at is not None:
            raise _session_not_found(session_id)
        return session


def _workspace_not_found(workspace_id: str) -> DomainError:
    return DomainError(
        code=ErrorCode.WORKSPACE_NOT_FOUND,
        message="Workspace was not found.",
        status_code=404,
        details={"workspace_id": workspace_id},
    )


def _runtime_unavailable(workspace_id: str) -> DomainError:
    return DomainError(
        code=ErrorCode.RUNTIME_UNAVAILABLE,
        message="Runtime event stream is unavailable.",
        status_code=503,
        details={"workspace_id": workspace_id},
    )


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, DomainError):
        return exc.code
    return ErrorCode.INTERNAL_ERROR.value


def _error_message(exc: BaseException) -> str:
    if isinstance(exc, DomainError):
        return exc.message
    return "turn execution failed"


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
