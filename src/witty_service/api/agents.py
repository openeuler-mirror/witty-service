from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import StreamingResponse

from witty_service.api.auth import require_bearer_auth
from witty_service.api.schemas import (
    AgentResponse,
    AgentSkillResponse,
    AgentWithConversationsResponse,
    ConversationDetailResponse,
    ConversationSummaryResponse,
    CreateAgentHubRequest,
    CreateAgentRequest,
    InstallAgentSkillRequest,
    McpServerResponse,
    UninstallAgentSkillRequest,
    MessageEventsResponse,
    SendMessageRequest,
    SessionEventsResponse,
    SessionResponse,
    UpdateConversationRequest,
)
from witty_service.api.services import ServiceContainer
from witty_service.adapter.http_client import AdaptorHttpClient
from witty_service.application.agent_manager import AGENT_NOT_FOUND, SKILL_NOT_FOUND, \
SKILL_INSTALL_RECORD_FAILED, SKILL_UNINSTALL_RECORD_FAILED, SKILL_SYNC_FAILED, AgentCreateRequest
from witty_service.application.agent_template_service import AgentTemplateService
from witty_service.application.skill_manager import SkillManager
from witty_service.domain.enums import AgentStatus
from witty_service.domain.errors import DomainError
from witty_service.persistence.repositories import AgentRecord

router = APIRouter(prefix="/agents", tags=["agents"], dependencies=[Depends(require_bearer_auth)])
logger = logging.getLogger(__name__)


def get_services(request: Request) -> ServiceContainer:
    return request.app.state.services


@router.post("", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
def create_agent(
    payload: CreateAgentRequest,
    services: ServiceContainer = Depends(get_services),
) -> AgentResponse:
    manager = services.get_agent_manager_for_sandbox(payload.sandbox_type)
    result = manager.create_agent(
        AgentCreateRequest(
            name=payload.name,
            description=payload.description,
            sandbox_type=payload.sandbox_type,
            adapter_type=payload.adapter_type,
            idle_timeout_seconds=payload.idle_timeout_seconds,
            sandbox_id=payload.sandbox_id,
            has_scheduled_tasks=payload.has_scheduled_tasks,
            model_id=payload.model_id,
            mcp_server_list=payload.mcp_server_list,
        )
    )

    # Extract port for local_process sandbox
    process_port: int | None = None
    if payload.sandbox_type == "local_process":
        sandbox_state = services.repository.get_sandbox_state(result.agent.id)
        if sandbox_state is not None:
            process_port = sandbox_state.sandbox_payload_json.get("metadata", {}).get("port")

    return _to_agent_response(result.agent, default_session_id=None, process_port=process_port)


@router.post("/agenthub", response_model=AgentResponse, status_code=status.HTTP_201_CREATED)
def create_agent_from_agenthub(
    payload: CreateAgentHubRequest,
    services: ServiceContainer = Depends(get_services),
) -> AgentResponse:
    """从远程 git 仓库拉取 agent 模板（agent.yaml），解析并创建 agent。"""
    template_service = AgentTemplateService(
        repository=services.repository,
        agent_manager_factory=services.get_agent_manager_for_sandbox,
    )
    result = template_service.create_agent_from_template(
        git_url=payload.git_url,
        branch=payload.branch,
        sandbox_type=payload.sandbox_type,
        adapter_type=payload.adapter_type,
        idle_timeout_seconds=payload.idle_timeout_seconds,
        sandbox_id=payload.sandbox_id,
        has_scheduled_tasks=payload.has_scheduled_tasks,
        model_id=payload.model_id,
    )

    # Extract port for local_process sandbox
    process_port: int | None = None
    if payload.sandbox_type == "local_process":
        sandbox_state = services.repository.get_sandbox_state(result.agent.id)
        if sandbox_state is not None:
            process_port = sandbox_state.sandbox_payload_json.get("metadata", {}).get("port")

    return _to_agent_response(result.agent, default_session_id=None, process_port=process_port)


@router.get("", response_model=list[AgentResponse] | list[AgentWithConversationsResponse])
def list_agents(
    include_conversations: bool = False,
    services: ServiceContainer = Depends(get_services),
) -> list[AgentResponse] | list[AgentWithConversationsResponse]:
    """列出 agent，并补充默认会话与 runtime skills 信息。

    当 ``include_conversations=true`` 时，每个agent会附带其conversations摘要
    """
    if include_conversations:
        enriched = services.repository.list_agents_with_conversations()
        result = []
        for item in enriched:
            manager = services.get_agent_manager_for_agent(item["id"])
            agent = manager._check_and_update_agent_status_if_needed(item["id"])

            process_port: int | None = None
            if agent.sandbox_type == "local_process" and agent.status != AgentStatus.error:
                sandbox_state = services.repository.get_sandbox_state(agent.id)
                if sandbox_state is not None:
                    process_port = sandbox_state.sandbox_payload_json.get("metadata", {}).get("port")

            sessions = services.session_manager.list_sessions(agent.id)
            default_session_id = sessions[0].id if sessions else None

            base = _to_agent_response(
                agent,
                default_session_id=default_session_id,
                process_port=process_port,
            )
            result.append(
                AgentWithConversationsResponse(
                    **base.model_dump(),
                    conversations=[ConversationSummaryResponse(**c) for c in item["conversations"]],
                )
            )
        return result

    agents = services.repository.list_agents()
    result = []
    for agent in agents:
        manager = services.get_agent_manager_for_agent(agent.id)

        # 检查沙箱健康状态，如果进程停止则更新 agent 状态为 error
        agent = manager._check_and_update_agent_status_if_needed(agent.id)

        sessions = services.session_manager.list_sessions(agent.id)
        default_session_id = sessions[0].id if sessions else None

        # Extract port for local_process sandbox
        process_port: int | None = None
        if agent.sandbox_type == "local_process" and agent.status != AgentStatus.error:
            sandbox_state = services.repository.get_sandbox_state(agent.id)
            if sandbox_state is not None:
                process_port = sandbox_state.sandbox_payload_json.get("metadata", {}).get("port")

        result.append(
            _to_agent_response(
                agent,
                default_session_id=default_session_id,
                process_port=process_port,
            )
        )
    return result


@router.get("/{agent_id}", response_model=AgentResponse)
def get_agent(agent_id: str, services: ServiceContainer = Depends(get_services)) -> AgentResponse:
    """获取单个 agent，并补充默认会话与 runtime skills 信息。"""
    manager = services.get_agent_manager_for_agent(agent_id)

    # 检查沙箱健康状态，如果进程停止则更新 agent 状态为 error
    agent = manager._check_and_update_agent_status_if_needed(agent_id)

    if agent.status == AgentStatus.error:
        # 沙箱进程已停止
        sessions = services.session_manager.list_sessions(agent_id)
        default_session_id = sessions[0].id if sessions else None
        return _to_agent_response(agent, default_session_id=default_session_id, process_port=None)

    if agent is None:
        raise DomainError(
            code=AGENT_NOT_FOUND,
            message="Agent was not found.",
            details={"agent_id": agent_id},
        )
    sessions = services.session_manager.list_sessions(agent_id)
    default_session_id = sessions[0].id if sessions else None

    # Extract port for local_process sandbox
    process_port: int | None = None
    if agent.sandbox_type == "local_process":
        sandbox_state = services.repository.get_sandbox_state(agent_id)
        if sandbox_state is not None:
            process_port = sandbox_state.sandbox_payload_json.get("metadata", {}).get("port")

    return _to_agent_response(agent, default_session_id=default_session_id, process_port=process_port)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: str, services: ServiceContainer = Depends(get_services)) -> Response:
    manager = services.get_agent_manager_for_agent(agent_id)
    await manager.delete_agent(agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{agent_id}/conversations", response_model=list[ConversationSummaryResponse])
def list_conversations(
    agent_id: str,
    services: ServiceContainer = Depends(get_services),
) -> list[ConversationSummaryResponse]:
    summaries = services.repository.list_sessions_with_summary(agent_id)
    return [ConversationSummaryResponse(**s) for s in summaries]


@router.get("/{agent_id}/conversations/{session_id}", response_model=ConversationDetailResponse)
def get_conversation(
    agent_id: str,
    session_id: str,
    limit: int = 50,
    before: str | None = None,
    services: ServiceContainer = Depends(get_services),
) -> ConversationDetailResponse:
    session = services.session_manager.get_session(agent_id, session_id)
    messages, has_more = services.repository.get_messages_with_events(
        session_id, limit=limit, before=before
    )
    return ConversationDetailResponse(
        id=session.id,
        agent_id=session.agent_id,
        title=session.title,
        pinned=session.pinned,
        status=session.status,
        messages=messages,
        has_more=has_more,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


@router.patch("/{agent_id}/conversations/{session_id}", response_model=SessionResponse)
def update_conversation(
    agent_id: str,
    session_id: str,
    payload: UpdateConversationRequest,
    services: ServiceContainer = Depends(get_services),
) -> SessionResponse:
    services.session_manager.get_session(agent_id, session_id)
    updated = services.repository.update_session_metadata(
        session_id,
        title=payload.title,
        pinned=payload.pinned,
    )
    return SessionResponse(
        id=updated.id,
        agent_id=updated.agent_id,
        status=updated.status,
        title=updated.title,
        pinned=updated.pinned,
        created_at=updated.created_at,
        updated_at=updated.updated_at,
    )


@router.post("/{agent_id}/pause", response_model=AgentResponse)
def pause_agent(agent_id: str, services: ServiceContainer = Depends(get_services)) -> AgentResponse:
    """暂停 agent。"""
    manager = services.get_agent_manager_for_agent(agent_id)
    agent = manager.pause_agent(agent_id)
    sessions = services.session_manager.list_sessions(agent_id)
    default_session_id = sessions[0].id if sessions else None
    return _to_agent_response(agent, default_session_id=default_session_id)


@router.post("/{agent_id}/resume", response_model=AgentResponse)
async def resume_agent(agent_id: str, services: ServiceContainer = Depends(get_services)) -> AgentResponse:
    """恢复 agent。"""
    manager = services.get_agent_manager_for_agent(agent_id)
    agent = await manager.resume_agent(agent_id)
    sessions = services.session_manager.list_sessions(agent_id)
    default_session_id = sessions[0].id if sessions else None
    return _to_agent_response(agent, default_session_id=default_session_id)


@router.get("/{agent_id}/sessions", response_model=list[SessionResponse])
async def list_sessions(
    agent_id: str,
    runtime_agent_id: str | None = None,
    services: ServiceContainer = Depends(get_services),
) -> list[SessionResponse]:
    manager = services.get_agent_manager_for_agent(agent_id)
    sessions = await manager.list_sessions(agent_id, runtime_agent_id=runtime_agent_id)
    return [SessionResponse.model_validate(session) for session in sessions]


@router.post(
    "/{agent_id}/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    agent_id: str,
    runtime_agent_id: str | None = None,
    services: ServiceContainer = Depends(get_services),
) -> SessionResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    session = await manager.create_session(agent_id, runtime_agent_id=runtime_agent_id)
    return SessionResponse.model_validate(session)


@router.get("/{agent_id}/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    agent_id: str,
    session_id: str,
    runtime_agent_id: str | None = None,
    services: ServiceContainer = Depends(get_services),
) -> SessionResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    session = await manager.get_session(agent_id, session_id, runtime_agent_id=runtime_agent_id)
    return SessionResponse.model_validate(session)


@router.get("/{agent_id}/sessions/{session_id}/events")
async def get_session_events(
    agent_id: str,
    session_id: str,
    offset: int = 0,
    limit: int = 50,
    runtime_agent_id: str | None = None,
    services: ServiceContainer = Depends(get_services),
) -> SessionEventsResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    result = await manager.get_session_events(
        agent_id=agent_id,
        session_id=session_id,
        offset=offset,
        limit=limit,
        runtime_agent_id=runtime_agent_id,
    )
    return SessionEventsResponse.model_validate(result)


@router.delete("/{agent_id}/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    agent_id: str,
    session_id: str,
    runtime_agent_id: str | None = None,
    services: ServiceContainer = Depends(get_services),
) -> Response:
    manager = services.get_agent_manager_for_agent(agent_id)
    await manager.delete_session(agent_id, session_id, runtime_agent_id=runtime_agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/{agent_id}/sessions/{session_id}/abort",
    response_model=None,
)
async def abort_session(
    agent_id: str,
    session_id: str,
    runtime_agent_id: str | None = None,
    services: ServiceContainer = Depends(get_services),
) -> dict[str, object]:
    manager = services.get_agent_manager_for_agent(agent_id)
    return await manager.abort_session(agent_id, session_id, runtime_agent_id=runtime_agent_id)


@router.post(
    "/{agent_id}/sessions/{session_id}/messages",
    response_model=MessageEventsResponse,
)
async def send_message(
    agent_id: str,
    session_id: str,
    payload: SendMessageRequest,
    services: ServiceContainer = Depends(get_services),
) -> MessageEventsResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    result = await manager.send_message(agent_id=agent_id, session_id=session_id, content=payload.content)
    return MessageEventsResponse.model_validate(result)


@router.post(
    "/{agent_id}/sessions/{session_id}/messages/stream",
    response_class=StreamingResponse,
)
async def send_message_stream(
    agent_id: str,
    session_id: str,
    payload: SendMessageRequest,
    services: ServiceContainer = Depends(get_services),
) -> StreamingResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    event_stream = manager.send_message_stream(
        agent_id=agent_id,
        session_id=session_id,
        content=payload.content,
    )
    return await _build_sse_streaming_response(event_stream)


@router.post(
    "/{agent_id}/sessions/{session_id}/messages/stream/reconnect",
    response_class=StreamingResponse,
)
async def reconnect_message_stream(
    agent_id: str,
    session_id: str,
    services: ServiceContainer = Depends(get_services),
) -> StreamingResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    event_stream = manager.reconnect_stream(
        agent_id=agent_id,
        session_id=session_id,
    )
    return await _build_sse_streaming_response(event_stream)



@router.post("/{agent_id}/skills/", response_model=AgentSkillResponse, status_code=status.HTTP_202_ACCEPTED)
async def install_agent_skill(
    agent_id: str,
    payload: InstallAgentSkillRequest,
    services: ServiceContainer = Depends(get_services),
) -> AgentSkillResponse:
    skill_manager = SkillManager(repository=services.repository)
    skill = skill_manager.get_skill_by_skill_id(payload.skill_id)
    if skill is None:
        raise DomainError(
            code=SKILL_NOT_FOUND,
            message="Skill was not found.",
            details={"skill_name": payload.skill_name, "skill_id": payload.skill_id},
        )

    skill_repo = skill_manager.get_repository_by_repo_id(skill.repo_id)
    skill_source_path = skill_manager.get_skill_source_path(skill)

    agent_manager = services.get_agent_manager_for_agent(agent_id)
    install_result = await agent_manager.install_agent_skill(
        agent_id,
        skill.skill_name,
        source_path=skill_source_path,
    )
    logger.info(
        (
            "Install skill dispatched successfully: agent_id=%s skill_name=%s "
            "skill_id=%s source_type=%s skill_source_path=%s result_keys=%s"
        ),
        agent_id,
        payload.skill_name,
        payload.skill_id,
        skill_repo.source_type,
        skill_source_path,
        sorted(install_result.keys()) if isinstance(install_result, dict) else [],
    )
    try:
        installed_record = services.repository.upsert_installed_agent_skill(
            agent_id=agent_id,
            skill_id=skill.skill_id,
            source_type=skill_repo.source_type,
            repo_id=skill_repo.repo_id,
            skill_name=skill.skill_name,
            relative_path=skill.relative_path,
            metadata=skill.metadata,
            skill_source=skill.skill_source,
            skill_md_url=skill.skill_md_url,
        )
    except Exception as exc:
        raise DomainError(
            code=SKILL_INSTALL_RECORD_FAILED,
            message="Skill was installed but failed to persist the install record.",
            details={
                "agent_id": agent_id,
                "repo_id": skill.repo_id,
                "skill_id": skill.skill_id,
                "error": str(exc),
            },
        ) from exc

    return _to_agent_skill_response(installed_record)


@router.get("/{agent_id}/skills/installed", response_model=list[AgentSkillResponse])
def list_installed_agent_skills(
    agent_id: str,
    services: ServiceContainer = Depends(get_services),
) -> list[AgentSkillResponse]:
    """查询指定 agent 已安装的技能记录。"""
    agent = services.repository.get_agent(agent_id)
    if agent is None:
        raise DomainError(
            code=AGENT_NOT_FOUND,
            message="Agent was not found.",
            details={"agent_id": agent_id},
        )

    records = services.repository.list_installed_agent_skills(agent_id)
    return [_to_agent_skill_response(item) for item in records]


@router.post("/{agent_id}/skills/installed/sync", response_model=list[AgentSkillResponse])
def sync_installed_agent_skills(
    agent_id: str,
    services: ServiceContainer = Depends(get_services),
) -> list[AgentSkillResponse]:
    """主动从 runtime 拉取并同步已安装技能，然后返回最新列表。"""
    manager = services.get_agent_manager_for_agent(agent_id)
    try:
        manager.sync_installed_agent_skills(agent_id)
    except Exception as exc:
        raise DomainError(
            code=SKILL_SYNC_FAILED,
            message="Failed to sync installed skills from runtime.",
            details={"agent_id": agent_id, "error": str(exc)},
        ) from exc

    records = services.repository.list_installed_agent_skills(agent_id)
    return [_to_agent_skill_response(item) for item in records]


@router.post("/{agent_id}/skills/uninstall", response_model=AgentSkillResponse)
async def uninstall_agent_skill(
    agent_id: str,
    payload: UninstallAgentSkillRequest,
    services: ServiceContainer = Depends(get_services),
) -> AgentSkillResponse:
    installed_record = services.repository.get_installed_agent_skill(
        agent_id=agent_id,
        skill_id=payload.skill_id,
    )
    if installed_record is None:
        raise DomainError(
            code=SKILL_NOT_FOUND,
            message="Installed skill was not found.",
            details={"agent_id": agent_id, "skill_id": payload.skill_id},
        )

    skill_manager = SkillManager(repository=services.repository)
    skill = skill_manager.get_skill_by_skill_id(payload.skill_id)
    skill_source_path = skill_manager.get_skill_source_path(skill) if skill else None

    agent_manager = services.get_agent_manager_for_agent(agent_id)
    await agent_manager.uninstall_agent_skill(
        agent_id=agent_id,
        skill_name=installed_record.skill_name,
        source_type=installed_record.source_type,
        source_path=skill_source_path,
    )

    try:
        services.repository.delete_installed_agent_skill(
            agent_id=agent_id,
            skill_id=payload.skill_id,
        )
    except Exception as exc:
        raise DomainError(
            code=SKILL_UNINSTALL_RECORD_FAILED,
            message="Skill was uninstalled but failed to clean install records.",
            details={
                "agent_id": agent_id,
                "skill_id": payload.skill_id,
                "error": str(exc),
            },
        ) from exc

    return _to_agent_skill_response(installed_record)


def _to_agent_response(
    agent: AgentRecord,
    default_session_id: str | None,
    process_port: int | None = None,
) -> AgentResponse:
    """组装 agent API 响应。"""
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
        has_scheduled_tasks=agent.has_scheduled_tasks,
        model_id=agent.model_id,
        mcp_server_list=agent.mcp_server_list,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        default_session_id=default_session_id,
        process_port=process_port,
    )


def _to_agent_skill_response(item: Any) -> AgentSkillResponse:
    return AgentSkillResponse(
        agent_id=item.agent_id,
        skill_id=item.skill_id,
        source_type=item.source_type,
        repo_id=item.repo_id,
        skill_name=item.skill_name,
        installed_at=item.installed_at,
        relative_path=item.relative_path,
        metadata=item.metadata,
        skill_source=item.skill_source,
        skill_md_url=item.skill_md_url,
    )


async def _build_sse_streaming_response(event_stream: AsyncIterator[dict[str, Any]]) -> StreamingResponse:
    first_event = await _prefetch_first_event(event_stream)

    async def stream() -> AsyncIterator[str]:
        try:
            if first_event is None:
                return

            yield _format_sse_data(first_event)
            if first_event["event"]["type"] in {"message.completed", "turn.completed"}:
                return

            async for event in event_stream:
                yield _format_sse_data(event)

        except (GeneratorExit, asyncio.CancelledError):
            if hasattr(event_stream, "aclose"):
                await event_stream.aclose()
            raise

    return StreamingResponse(stream(), media_type="text/event-stream")


def _format_sse_data(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


async def _prefetch_first_event(
    event_stream: AsyncIterator[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        return await anext(event_stream)
    except StopAsyncIteration:
        return None


@router.post("/{agent_id}/mcp-servers/{server_id}/enable", response_model=McpServerResponse)
async def enable_mcp_server(
    agent_id: str,
    server_id: str,
    services: ServiceContainer = Depends(get_services),
) -> McpServerResponse:
    """为指定 Agent 启用 MCP Server，执行 _setup_mcp 将配置应用到 runtime。"""
    agent = services.repository.get_agent(agent_id)
    if agent is None:
        raise DomainError(
            code=AGENT_NOT_FOUND,
            message="Agent was not found.",
            details={"agent_id": agent_id},
        )
    
    mcp_server = services.repository.get_mcp_server(server_id)
    if mcp_server is None:
        raise DomainError(
            code="MCP_SERVER_NOT_FOUND",
            message="MCP Server was not found.",
            details={"server_id": server_id},
        )

    if agent.status != AgentStatus.running:
        raise DomainError(
            code="AGENT_NOT_RUNNING",
            message="Agent must be running to enable MCP Server.",
            details={"agent_id": agent_id, "status": agent.status.value},
        )

    adaptor_client = AdaptorHttpClient(
        base_url=services.repository.get_sandbox_state(agent_id).adapter_base_url
    )
    try:
        await adaptor_client.post(
            "/agent/mcp/enable",
            json={
                "mcp_server_name": mcp_server.mcp_server_name,
                "mcp_server_config": mcp_server.mcp_server_config,
            },
        )
    finally:
        await adaptor_client.close()

    if server_id not in agent.mcp_server_list:
        updated_list = list(agent.mcp_server_list)
        updated_list.append(server_id)
        services.repository.update_agent_mcp_server_list(agent_id, updated_list)

    return McpServerResponse(
        id=mcp_server.id,
        mcp_server_name=mcp_server.mcp_server_name,
        mcp_server_config=mcp_server.mcp_server_config,
        created_at=mcp_server.created_at,
        updated_at=mcp_server.updated_at,
    )


@router.post("/{agent_id}/mcp-servers/{server_id}/disable", response_model=McpServerResponse)
async def disable_mcp_server(
    agent_id: str,
    server_id: str,
    services: ServiceContainer = Depends(get_services),
) -> McpServerResponse:
    """为指定 Agent 卸载 MCP Server，执行 openclaw mcp unset 从 runtime 移除配置。"""
    agent = services.repository.get_agent(agent_id)
    if agent is None:
        raise DomainError(
            code=AGENT_NOT_FOUND,
            message="Agent was not found.",
            details={"agent_id": agent_id},
        )
    
    mcp_server = services.repository.get_mcp_server(server_id)
    if mcp_server is None:
        raise DomainError(
            code="MCP_SERVER_NOT_FOUND",
            message="MCP Server was not found.",
            details={"server_id": server_id},
        )

    if agent.status != AgentStatus.running:
        raise DomainError(
            code="AGENT_NOT_RUNNING",
            message="Agent must be running to disable MCP Server.",
            details={"agent_id": agent_id, "status": agent.status.value},
        )

    adaptor_client = AdaptorHttpClient(
        base_url=services.repository.get_sandbox_state(agent_id).adapter_base_url
    )
    try:
        await adaptor_client.post(
            "/agent/mcp/disable",
            json={
                "mcp_server_name": mcp_server.mcp_server_name,
            },
        )
    finally:
        await adaptor_client.close()

    if server_id in agent.mcp_server_list:
        updated_list = [id for id in agent.mcp_server_list if id != server_id]
        services.repository.update_agent_mcp_server_list(agent_id, updated_list)

    return McpServerResponse(
        id=mcp_server.id,
        mcp_server_name=mcp_server.mcp_server_name,
        mcp_server_config=mcp_server.mcp_server_config,
        created_at=mcp_server.created_at,
        updated_at=mcp_server.updated_at,
    )
