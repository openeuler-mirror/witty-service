import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from witty_agent_server.application.models.errors import ErrorResponse
from witty_agent_server.application.services.agent import (
    AgentService,
    AgentServiceError,
)
from witty_agent_server.application.services.skill.base import AgentSkillServiceBase
from witty_agent_server.application.services.skill.errors import (
    AgentSkillServiceError,
)


logger = logging.getLogger(__name__)


class InstallSkillRequest(BaseModel):
    skill_name: str = Field(min_length=1)
    source_type: str | None = None
    source_path: str | None = None
    skill_source: str | None = None


class UninstallSkillRequest(BaseModel):
    skill_name: str = Field(min_length=1)
    source_type: str | None = None
    source_path: str | None = None
    runtime_source: str | None = None


def create_agent_router(
    agent_service: AgentService,
    *,
    skill_service: AgentSkillServiceBase,
) -> APIRouter:
    router = APIRouter(prefix="/agent")

    @router.post("/start", response_model=None)
    async def start_agent(
        request: Request,
        id: str | None = None,
        reload: bool = True,
    ) -> dict[str, Any] | JSONResponse:
        try:
            body = await request.json()
            config = {
                "model_id": body.get("model_id"),
                "model": body.get("model"),
                "mcp_server_name": body.get("mcp_server_name"),
                "mcp_server_config": body.get("mcp_server_config"),
                "openclaw": body.get("openclaw"),
                "opencode": body.get("opencode")
            }
            response = agent_service.start(
                agent_id=id,
                config=config,
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
            resolved_agent_id = _resolve_request_agent_id(
                agent_service=agent_service,
                agent_id=id,
            )
            return agent_service.stop(agent_id=resolved_agent_id).model_dump()
        except AgentServiceError as exc:
            return _map_agent_error(request=request, exc=exc)

    @router.get("/status", response_model=None)
    def get_agent_status(
        request: Request,
        id: str | None = None,
    ) -> dict[str, Any] | JSONResponse:
        try:
            resolved_agent_id = _resolve_request_agent_id(
                agent_service=agent_service,
                agent_id=id,
            )
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
            resolved_agent_id = _resolve_request_agent_id(
                agent_service=agent_service,
                agent_id=id,
            )
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
            return skill_service.list_skills(agent_id=agent.id)
        except AgentSkillServiceError as exc:
            logger.warning(
                "get_agent_skills failed, runtime_type=%s code=%s",
                agent.runtime_type,
                exc.code,
            )
            return _map_agent_skill_error(request=request, exc=exc)

    @router.post("/skills/install", response_model=None)
    def install_agent_skill(
        payload: InstallSkillRequest,
        request: Request,
        id: str | None = None,
    ) -> dict[str, Any] | JSONResponse:
        try:
            resolved_agent_id = _resolve_request_agent_id(
                agent_service=agent_service,
                agent_id=id,
            )
            agent = agent_service.status(agent_id=resolved_agent_id)
        except AgentServiceError as exc:
            return _map_agent_error(request=request, exc=exc)
        logger.info(
            "install_agent_skill called, agent_id=%s runtime_type=%s skill_name=%s source_type=%s source_path=%s skill_source=%s",
            agent.id,
            agent.runtime_type,
            payload.skill_name,
            payload.source_type,
            payload.source_path,
            payload.skill_source,
        )
        try:
            install_result = skill_service.install_skill(
                agent_id=agent.id,
                skill_name=payload.skill_name,
                source_type=payload.source_type,
                source_path=payload.source_path,
                skill_source=payload.skill_source,
            )

            return {
                "agent_id": agent.id,
                "runtime_type": agent.runtime_type,
                **install_result,
            }
        except AgentSkillServiceError as exc:
            logger.warning(
                "install_agent_skill failed, runtime_type=%s message=%s",
                agent.runtime_type,
                exc.message,
            )
            return _map_agent_skill_error(request=request, exc=exc)
        except Exception as exc:
            logger.exception(
                "install_agent_skill unexpected error, agent_id=%s runtime_type=%s",
                agent.id,
                agent.runtime_type,
            )
            return _error_response(
                request=request,
                status_code=500,
                code="INTERNAL_SERVER_ERROR",
                message="internal server error",
                details={
                    "runtime_type": agent.runtime_type,
                    "reason": str(exc),
                },
            )

    @router.post("/skills/uninstall", response_model=None)
    def uninstall_agent_skill(
        payload: UninstallSkillRequest,
        request: Request,
        id: str | None = None,
    ) -> dict[str, Any] | JSONResponse:
        try:
            resolved_agent_id = _resolve_request_agent_id(
                agent_service=agent_service,
                agent_id=id,
            )
            agent = agent_service.status(agent_id=resolved_agent_id)
        except AgentServiceError as exc:
            return _map_agent_error(request=request, exc=exc)
        logger.info(
            "uninstall_agent_skill called, agent_id=%s runtime_type=%s skill_name=%s",
            agent.id,
            agent.runtime_type,
            payload.skill_name,
        )
        try:
            uninstall_result = skill_service.uninstall_skill(
                agent_id=agent.id,
                skill_name=payload.skill_name,
                source_type=payload.source_type,
                source_path=payload.source_path,
                runtime_source=payload.runtime_source,
            )
            return {
                "agent_id": agent.id,
                "runtime_type": agent.runtime_type,
                **uninstall_result,
            }
        except AgentSkillServiceError as exc:
            logger.warning(
                "uninstall_agent_skill failed, runtime_type=%s code=%s message=%s",
                agent.runtime_type,
                exc.code,
                exc.message,
            )
            return _map_agent_skill_error(request=request, exc=exc)
        except Exception as exc:
            logger.exception(
                "uninstall_agent_skill unexpected error, agent_id=%s runtime_type=%s",
                agent.id,
                agent.runtime_type,
            )
            return _error_response(
                request=request,
                status_code=500,
                code="INTERNAL_SERVER_ERROR",
                message="internal server error",
                details={
                    "runtime_type": agent.runtime_type,
                    "reason": str(exc),
                },
            )

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

    @router.post("/mcp/enable", response_model=None)
    async def enable_mcp(
        request: Request,
        id: str | None = None,
    ) -> dict[str, Any] | JSONResponse:
        """启用 MCP Server，执行 _setup_mcp 将配置应用到 runtime。"""
        try:
            body = await request.json()
            mcp_server_name = body.get("mcp_server_name")
            mcp_server_config = body.get("mcp_server_config")
            
            resolved_agent_id = _resolve_request_agent_id(
                agent_service=agent_service,
                agent_id=id,
            )
            agent_service.setup_mcp(
                agent_id=resolved_agent_id,
                mcp_server_name=mcp_server_name,
                mcp_server_config=mcp_server_config,
            )
            return {"status": "success", "mcp_server_name": mcp_server_name}
        except AgentServiceError as exc:
            logger.warning("enable_mcp failed, code=%s", exc.code)
            return _map_agent_error(request=request, exc=exc)
        except Exception as exc:
            logger.exception("enable_mcp unexpected error")
            return _error_response(
                request=request,
                status_code=500,
                code="INTERNAL_SERVER_ERROR",
                message="internal server error",
                details={"reason": str(exc)},
            )

    @router.post("/mcp/disable", response_model=None)
    async def disable_mcp(
        request: Request,
        id: str | None = None,
    ) -> dict[str, Any] | JSONResponse:
        """卸载 MCP Server，执行 openclaw mcp unset 从 runtime 移除配置。"""
        try:
            body = await request.json()
            mcp_server_name = body.get("mcp_server_name")
            
            resolved_agent_id = _resolve_request_agent_id(
                agent_service=agent_service,
                agent_id=id,
            )
            agent_service.unset_mcp(
                agent_id=resolved_agent_id,
                mcp_server_name=mcp_server_name,
            )
            return {"status": "success", "mcp_server_name": mcp_server_name}
        except AgentServiceError as exc:
            logger.warning("disable_mcp failed, code=%s", exc.code)
            return _map_agent_error(request=request, exc=exc)
        except Exception as exc:
            logger.exception("disable_mcp unexpected error")
            return _error_response(
                request=request,
                status_code=500,
                code="INTERNAL_SERVER_ERROR",
                message="internal server error",
                details={"reason": str(exc)},
            )

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


def _resolve_request_agent_id(
    *, agent_service: AgentService, agent_id: str | None
) -> str | None:
    """未显式传入 id 时，统一回退到 Gateway 默认 agent。"""
    if isinstance(agent_id, str) and agent_id:
        return agent_id
    return agent_service.resolve_default_agent()
