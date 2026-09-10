"""预置 Agent 模板端点（B4）：模板浏览 + 一键实例化。

- ``GET /agent-templates``：只读，扫包内元数据，零网络、零 DB（``source_commit`` 仅本地读缓存 HEAD）。
  同时返回模板声明的 MCP（``mcp_count``/``mcp_servers``）与默认使用提问（``default_prompt``）。
- ``POST /agent-templates/{name}/instantiate``：一模板一实例；复用 ``AgentTemplateService``
  （skill 缓存 B2 + 元数据扫描 B3 + 编排 B4），创建 agent 后安装 skills / AGENTS.md / opencode.json，
  任一步失败整体回滚（删除 agent 释放同名）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status

from witty_service.api.auth import require_bearer_auth
from witty_service.api.schemas import (
    AgentResponse,
    AgentTemplateInfoResponse,
    InstantiateAgentTemplateRequest,
)
from witty_service.api.services import ServiceContainer
from witty_service.application.agent_template_service import AgentTemplateService
from witty_service.persistence.repositories import AgentRecord

router = APIRouter(
    prefix="/agent-templates",
    tags=["agent-templates"],
    dependencies=[Depends(require_bearer_auth)],
)


def get_services(request: Request) -> ServiceContainer:
    return request.app.state.services


def _template_service(services: ServiceContainer) -> AgentTemplateService:
    return AgentTemplateService(
        repository=services.repository,
        agent_manager_factory=services.get_agent_manager_for_sandbox,
    )


@router.get("", response_model=list[AgentTemplateInfoResponse])
def list_agent_templates(
    services: ServiceContainer = Depends(get_services),
) -> list[AgentTemplateInfoResponse]:
    """列出包内预置模板（只读，零网络、零 DB）。"""
    service = _template_service(services)
    result: list[AgentTemplateInfoResponse] = []
    for template in AgentTemplateService.scan_preset_templates():
        source_commit = None
        if template.skill_source is not None:
            cache_dir = service.resolve_skill_cache_dir(
                template.skill_source.git_url,
                template.skill_source.branch,
            )
            source_commit = service.read_repo_commit(cache_dir)
        result.append(
            AgentTemplateInfoResponse(
                name=template.name,
                description=template.description,
                version=template.version,
                skill_count=len(template.skills),
                skills=[skill.name for skill in template.skills],
                mcp_count=len(template.mcp),
                mcp_servers=[server.name for server in template.mcp],
                default_prompt=template.prompt.default,
                source_commit=source_commit,
            )
        )
    return result


@router.post(
    "/{name}/instantiate",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def instantiate_agent_template(
    name: str,
    payload: InstantiateAgentTemplateRequest,
    services: ServiceContainer = Depends(get_services),
) -> AgentResponse:
    """一键实例化预置模板（一模板一实例，同名 agent 已存在返回 409）。"""
    service = _template_service(services)
    result = await service.instantiate_preset_template(
        name=name,
        model_id=payload.model_id,
        sandbox_type=payload.sandbox_type or "local_process",
    )
    return _to_agent_response(
        result.agent,
        sandbox_type=result.agent.sandbox_type,
        services=services,
    )


def _to_agent_response(
    agent: AgentRecord,
    *,
    sandbox_type: str,
    services: ServiceContainer,
) -> AgentResponse:
    """组装与 POST /agents 一致的 Agent 响应（含 process_port）。"""
    process_port: int | None = None
    if sandbox_type == "local_process":
        sandbox_state = services.repository.get_sandbox_state(agent.id)
        if sandbox_state is not None:
            process_port = sandbox_state.sandbox_payload_json.get("metadata", {}).get(
                "port"
            )
    return AgentResponse(
        id=agent.id,
        name=agent.name,
        description=agent.description,
        sandbox_type=agent.sandbox_type,
        adapter_type=agent.adapter_type,
        status=agent.status.value,
        sandbox_id=agent.sandbox_id,
        workspace_path=agent.workspace_path,
        idle_timeout_seconds=agent.idle_timeout_seconds,
        model_id=agent.model_id,
        mcp_server_list=agent.mcp_server_list,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        default_session_id=None,
        process_port=process_port,
    )
