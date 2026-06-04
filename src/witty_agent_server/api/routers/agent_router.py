import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from witty_agent_server.application.models.errors import ErrorResponse
from witty_agent_server.application.models.agent_start import AgentStartRequest
from witty_agent_server.application.services.agent import (
    AgentService,
    AgentServiceError,
)
from witty_agent_server.application.services.skill.base import AgentSkillServiceBase
from witty_agent_server.application.services.skill.errors import (
    AgentSkillServiceError,
)
from witty_agent_server.application.services.skill.openclaw_skill_service import (
    OpenClawSkillService,
)
from witty_agent_server.application.services.skill.opencode_skill_service import (
    OpenCodeSkillService,
)


logger = logging.getLogger(__name__)


def create_agent_router(
    agent_service: AgentService,
    openclaw_skill_service: OpenClawSkillService | None = None,
    opencode_skill_service: OpenCodeSkillService | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/agent")
    resolved_openclaw_skill_service = openclaw_skill_service or OpenClawSkillService()
    resolved_opencode_skill_service = opencode_skill_service or OpenCodeSkillService()

    @router.post("/start", response_model=None)
    def start_agent(
        request: Request,
        body: AgentStartRequest | None = None,
        reload: bool = False,
    ) -> dict[str, Any] | JSONResponse:
        try:
            start_config = _build_start_config(body=body)
            agent_id = start_config.agent_id if start_config is not None else None
            _require_agent_id(agent_id=agent_id)
            _require_runtime_type(start_config=start_config)
            logger.info(
                "start_agent called, agent_id=%s runtime_type=%s deployment_mode=%s reload=%s",
                agent_id,
                start_config.runtime_type if start_config is not None else None,
                start_config.deployment_mode if start_config is not None else None,
                reload,
            )

            response = agent_service.start(
                config=start_config,
                reload=reload,
            ).model_dump()
            response["already_running"] = agent_service.last_start_already_running
            return response
        except AgentServiceError as exc:
            return _map_agent_error(request=request, exc=exc)

    @router.post("/stop", response_model=None)
    def stop_agent(
        request: Request,
        id: str | None = None,
    ) -> dict[str, Any] | JSONResponse:
        try:
            resolved_agent_id = _require_agent_id(agent_id=id)
            return agent_service.stop(agent_id=resolved_agent_id).model_dump()
        except AgentServiceError as exc:
            return _map_agent_error(request=request, exc=exc)

    @router.get("/status", response_model=None)
    def get_agent_status(
        request: Request,
        id: str | None = None,
    ) -> dict[str, Any] | JSONResponse:
        try:
            resolved_agent_id = _require_agent_id(agent_id=id)
            agent = agent_service.status(agent_id=resolved_agent_id)
        except AgentServiceError as exc:
            return _map_agent_error(request=request, exc=exc)
        return {
            "id": agent.id,
            "status": agent.status,
            "runtime_type": agent.runtime_type,
        }

    @router.get("/skills", response_model=None)
    def get_agent_skills(
        request: Request,
        id: str | None = None,
    ) -> dict[str, Any] | JSONResponse:
        try:
            resolved_agent_id = _require_agent_id(agent_id=id)
            agent = agent_service.status(agent_id=resolved_agent_id)
        except AgentServiceError as exc:
            return _map_agent_error(request=request, exc=exc)
        logger.info(
            "get_agent_skills called, agent_id=%s runtime_type=%s status=%s",
            agent.id,
            agent.runtime_type,
            agent.status,
        )
        try:
            resolved_skill_service = _resolve_skill_service(
                runtime_type=agent.runtime_type,
                openclaw_skill_service=resolved_openclaw_skill_service,
                opencode_skill_service=resolved_opencode_skill_service,
            )
            return resolved_skill_service.list_skills(agent_id=agent.id)
        except AgentSkillServiceError as exc:
            logger.warning(
                "get_agent_skills failed, runtime_type=%s code=%s",
                agent.runtime_type,
                exc.code,
            )
            return _map_agent_skill_error(request=request, exc=exc)

    @router.get("/list", response_model=None)
    def list_agents(
        request: Request,
    ) -> dict[str, Any] | JSONResponse:
        """查询当前 runtime 可见的 agent 列表，并原样透传 gateway 响应。"""
        try:
            logger.info("list_agents called")
            return agent_service.list_agents()
        except AgentServiceError as exc:
            logger.warning("list_agents failed, code=%s", exc.code)
            return _map_agent_error(request=request, exc=exc)

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
        request_id=_get_request_id(request),
        details=details,
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(exclude_none=True),
    )


def _get_request_id(request: Request) -> str:
    header_value = request.headers.get("x-request-id")
    if isinstance(header_value, str) and header_value:
        return header_value
    return str(uuid4())


def _map_agent_error(*, request: Request, exc: AgentServiceError) -> JSONResponse:
    return _error_response(
        request=request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


def _map_agent_skill_error(
    *, request: Request, exc: AgentSkillServiceError
) -> JSONResponse:
    return _error_response(
        request=request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


def _resolve_skill_service(
    *,
    runtime_type: str,
    openclaw_skill_service: OpenClawSkillService,
    opencode_skill_service: OpenCodeSkillService,
) -> AgentSkillServiceBase:
    if runtime_type == "openclaw":
        return openclaw_skill_service
    if runtime_type == "opencode":
        return opencode_skill_service
    raise AgentSkillServiceError(
        code="RUNTIME_SKILLS_NOT_SUPPORTED",
        message="runtime skills query is not supported",
        status_code=501,
        details={"runtime_type": runtime_type},
    )


def _require_agent_id(*, agent_id: str | None) -> str:
    """校验并返回请求中的 agent_id，不允许默认回退。"""
    if isinstance(agent_id, str) and agent_id:
        return agent_id
    raise AgentServiceError(
        code="AGENT_ID_REQUIRED",
        message="agent_id is required",
        status_code=400,
        details=None,
    )


def _build_start_config(body: AgentStartRequest | None) -> AgentStartRequest | None:
    """构建 start 请求对象，统一调用链模型。"""
    if body is None:
        return None
    start_request = AgentStartRequest(
        agent_id=body.agent_id,
        runtime_type=body.runtime_type,
        deployment_mode=body.deployment_mode,
    )
    if (
        start_request.runtime_type is None
        and start_request.deployment_mode is None
        and start_request.agent_id is None
    ):
        return None
    return start_request


def _require_runtime_type(*, start_config: AgentStartRequest | None) -> str:
    """校验 start 请求中的 runtime_type 必填。"""
    runtime_type = start_config.runtime_type if start_config is not None else None
    if isinstance(runtime_type, str) and runtime_type:
        return runtime_type
    raise AgentServiceError(
        code="RUNTIME_TYPE_REQUIRED",
        message="runtime_type is required",
        status_code=400,
        details=None,
    )
