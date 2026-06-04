from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from witty_agent_server.application.models.errors import ErrorResponse
from witty_agent_server.application.services.agent import (
    AgentService,
    AgentServiceError,
)
from witty_agent_server.application.services.session_manage.base import (
    SessionStateEventPublisherBase,
)
from witty_agent_server.application.services.session_manage.state_sync_service import (
    SessionStateSyncService,
)
from witty_agent_server.application.services.session_to_runtime import (
    SessionNotFoundServiceError,
    SessionService,
    SessionServiceError,
)
from witty_agent_server.infra.persistence.in_memory import InMemorySessionRepository
from witty_agent_server.runtimes.openclaw_runtime_factory import create_openclaw_runtime


def build_default_session_service() -> SessionService:
    from witty_agent_server.application.services.session_to_runtime import (
        openclaw_session_service,
        runtime_registry,
    )

    runtime_registry_instance = runtime_registry.RuntimeRegistry()
    runtime = create_openclaw_runtime()
    runtime_registry_instance.register(runtime)
    openclaw_service = openclaw_session_service.OpenClawSessionService(
        runtime_registry=runtime_registry_instance,
        repository=InMemorySessionRepository(),
    )
    openclaw_service.register_runtime(runtime)
    return SessionService(
        services={"openclaw": openclaw_service},
    )


def create_session_router(
    session_service: SessionService | None = None,
    state_sync_service: SessionStateEventPublisherBase | None = None,
    agent_service: AgentService | None = None,
) -> APIRouter:
    service = session_service or build_default_session_service()
    resolved_state_sync = state_sync_service or SessionStateSyncService()
    router = APIRouter(prefix="/agents/{agent_id}/sessions")

    @router.post("", response_model=None)
    def create_session(agent_id: str, request: Request) -> dict[str, Any] | JSONResponse:
        try:
            resolved_agent_config = _resolve_agent_config(
                agent_id=agent_id,
                agent_service=agent_service,
            )
            return service.create_session(
                agent_id=agent_id,
                config=resolved_agent_config,
            )
        except AgentServiceError as exc:
            return _map_agent_error(request=request, exc=exc)
        except SessionServiceError as exc:
            return _map_session_error(request=request, exc=exc)

    @router.post("/{session_id}/delete", response_model=None)
    def delete_session(
        agent_id: str, session_id: str, request: Request
    ) -> dict[str, Any] | JSONResponse:
        try:
            return service.delete_session(agent_id=agent_id, session_id=session_id)
        except SessionServiceError as exc:
            return _map_session_error(request=request, exc=exc)

    @router.post("/{session_id}/abort", response_model=None)
    def abort_session(
        agent_id: str, session_id: str, request: Request
    ) -> dict[str, Any] | JSONResponse:
        try:
            session = service.get_session(agent_id=agent_id, session_id=session_id)
            result = service.abort_session(agent_id=agent_id, session_id=session_id)
        except SessionServiceError as exc:
            return _map_session_error(request=request, exc=exc)
        if isinstance(session, dict):
            runtime_type = session.get("runtime_type")
            if isinstance(runtime_type, str):
                # abort 成功后会话进入空闲态，实时推送 idle。
                resolved_state_sync.emit_state_changed(
                    agent_id=agent_id,
                    session_id=session_id,
                    runtime_type=runtime_type,
                    state="idle",
                    reason="session.abort",
                )
        return result

    @router.get("")
    def list_sessions(agent_id: str, request: Request) -> dict[str, Any]:
        del request
        sessions = service.list_sessions(agent_id=agent_id)
        return {"sessions": sessions}

    @router.get("/{session_id}", response_model=None)
    def get_session(
        agent_id: str, session_id: str, request: Request
    ) -> dict[str, Any] | JSONResponse:
        try:
            session = service.get_session(agent_id=agent_id, session_id=session_id)
        except SessionServiceError as exc:
            return _map_session_error(request=request, exc=exc)
        if session is None:
            return _map_session_error(
                request=request,
                exc=SessionNotFoundServiceError(),
            )
        return session

    @router.get("/{session_id}/events")
    def get_session_events(
        agent_id: str,
        session_id: str,
        request: Request,
        offset: int = Query(default=0),
        limit: int = Query(default=50),
    ) -> Any:
        try:
            return service.list_events(
                agent_id=agent_id,
                session_id=session_id,
                offset=offset,
                limit=limit,
            )
        except SessionServiceError as exc:
            return _map_session_error(request=request, exc=exc)

    return router


def _error_response(
    *,
    request: Request,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body = ErrorResponse(
        code=code,
        message=message,
        details=details,
        request_id=_get_request_id(request),
    )
    return JSONResponse(
        status_code=status_code, content=body.model_dump(exclude_none=True)
    )


def _get_request_id(request: Request) -> str:
    header_value = request.headers.get("x-request-id")
    if isinstance(header_value, str) and header_value:
        return header_value
    return str(uuid4())


def _map_session_error(*, request: Request, exc: SessionServiceError) -> JSONResponse:
    return _error_response(
        request=request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


def _map_agent_error(*, request: Request, exc: AgentServiceError) -> JSONResponse:
    return _error_response(
        request=request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


def _resolve_agent_config(
    *, agent_id: str, agent_service: AgentService | None
) -> dict[str, Any]:
    """在组合层按显式 agent_id 读取配置，避免 SessionService 依赖 AgentService。"""
    if agent_service is None:
        return {}
    agent = agent_service.status(agent_id=agent_id)
    return dict(agent.config)
