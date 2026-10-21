from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from witty_agent_server.api.routers.agent_router import create_agent_router
from witty_agent_server.api.routers.session_router import create_session_router
from witty_agent_server.api.routers.session_ws_router import create_session_ws_router
from witty_agent_server.application.models.errors import ErrorResponse
from witty_agent_server.application.runtime_factory import create_opencode_bundle
from witty_agent_server.application.services.agent import AgentService
from witty_agent_server.application.services.session import SessionService
from witty_agent_server.application.services.session_state_sync_service import (
    SessionStateSyncService,
)
from witty_agent_server.application.services.session_ws_orchestrator import (
    SessionWSOrchestrator,
)
from witty_agent_server.application.services.task_pool import TaskPool
from witty_agent_server.logger.logging_config import configure_logging
from witty_service.config import get_settings

base_router = APIRouter()


@base_router.get("/ping")
def ping() -> dict[str, str]:
    return {"status": "ok"}


def create_app(
    session_service: SessionService | None = None,
    *,
    agent_service: AgentService | None = None,
) -> FastAPI:
    """创建 witty-agent-server FastAPI 应用。

    每个 agent-server 实例固定对应 OpenCode runtime；
    ``session_service`` / ``agent_service`` 可覆盖默认装配用于测试。
    """
    configure_logging()

    settings = get_settings()

    # 单 runtime 装配：一个 agent-server = 一份 OpenCode 组件树。
    assembly = create_opencode_bundle()

    resolved_agent_service = agent_service or assembly.agent_service
    resolved_session_service = session_service or assembly.session_service

    session_state_sync_service = SessionStateSyncService()
    session_ws_orchestrator = SessionWSOrchestrator(
        session_service=resolved_session_service,
        agent_service=resolved_agent_service,
        state_sync_service=session_state_sync_service,
        runtime_type="opencode",
    )

    task_pool = TaskPool(orchestrator=session_ws_orchestrator)

    app = FastAPI(title="Witty Agent Server")

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        body = ErrorResponse(
            code="REQUEST_VALIDATION_ERROR",
            message="request validation failed",
            request_id=_get_request_id(request),
            details={"errors": exc.errors()},
        )
        return JSONResponse(status_code=422, content=body.model_dump(exclude_none=True))

    app.include_router(base_router)
    app.include_router(
        create_agent_router(
            resolved_agent_service,
            skill_service=assembly.skill_service,
        )
    )
    app.include_router(
        create_session_router(
            resolved_session_service,
            state_sync_service=session_state_sync_service,
            agent_service=resolved_agent_service,
        )
    )
    app.include_router(
        create_session_ws_router(
            task_pool=task_pool,
            state_sync_service=session_state_sync_service,
        )
    )

    @app.get("/server/capabilities")
    def capabilities() -> dict[str, list[str]]:
        # supported: 镜像能力声明；当前实例实际运行 resolved_bundle.runtime_type
        return {"supported_runtimes": list(settings.runtime.supported)}

    return app


def _get_request_id(request: Request) -> str:
    header_value = request.headers.get("x-request-id")
    if isinstance(header_value, str) and header_value:
        return header_value
    return str(uuid4())
