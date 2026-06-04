import os
from collections.abc import Callable
from uuid import uuid4

from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from witty_agent_server.application.composition import AppContainer
from witty_agent_server.application.models.errors import ErrorResponse
from witty_agent_server.application.services.agent import AgentService
from witty_agent_server.application.services.session_to_runtime import SessionService
from witty_agent_server.logger.logging_config import configure_logging
from witty_agent_server.runtimes.runtime_base import RuntimeBase, RuntimeType


base_router = APIRouter()


@base_router.get("/ping")
def ping() -> dict[str, str]:
    return {"status": "ok"}


def create_app(
    session_service: SessionService | None = None,
    *,
    agent_service: AgentService | None = None,
    base_root: str | None = None,
    runtime_factories: dict[RuntimeType, Callable[[], RuntimeBase]] | None = None,
) -> FastAPI:
    """创建应用实例：日志、容器、router、异常处理。"""
    configure_logging()

    # 优先级: 显式参数 > WITTY_WORKSPACE_BASE 环境变量 > AppContainer 默认值
    if base_root is None:
        base_root = os.environ.get("WITTY_WORKSPACE_BASE")

    container = AppContainer.build(
        base_root=base_root,
        agent_service=agent_service,
        session_service=session_service,
        runtime_factories=runtime_factories,
    )

    app = FastAPI(title="Witty Agent Server")
    _register_exception_handlers(app)

    app.include_router(base_router)
    app.include_router(_create_capabilities_router(container))
    app.include_router(container.create_agent_router())
    app.include_router(container.create_session_router())
    app.include_router(container.create_session_ws_router())
    return app


def _create_capabilities_router(container: AppContainer) -> APIRouter:
    """创建按容器实际装配结果返回能力的路由。"""
    router = APIRouter()

    @router.get("/server/capabilities")
    def capabilities() -> dict[str, list[str]]:
        return {
            "supported_runtimes": sorted(container.runtime_factories),
        }

    return router


def _register_exception_handlers(app: FastAPI) -> None:
    """注册应用级异常处理器。"""

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


def _get_request_id(request: Request) -> str:
    header_value = request.headers.get("x-request-id")
    if isinstance(header_value, str) and header_value:
        return header_value
    return str(uuid4())
