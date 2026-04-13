from __future__ import annotations

import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import StreamingResponse

from src.api.auth import require_bearer_auth
from src.api.schemas import (
    AgentResponse,
    CreateAgentRequest,
    MessageEventsResponse,
    SendMessageRequest,
    SessionEventsResponse,
    SessionResponse,
)
from src.api.services import ServiceContainer
from src.application.agent_manager import AGENT_NOT_FOUND, AgentCreateRequest
from src.domain.errors import DomainError
from src.persistence.repositories import AgentRecord

router = APIRouter(prefix="/api/v1/agents", tags=["agents"], dependencies=[Depends(require_bearer_auth)])


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
        )
    )
    return _to_agent_response(result.agent, default_session_id=result.default_session.id)


@router.get("", response_model=list[AgentResponse])
def list_agents(services: ServiceContainer = Depends(get_services)) -> list[AgentResponse]:
    agents = services.repository.list_agents()
    return [
        _to_agent_response(
            agent,
            default_session_id=(
                sessions[0].id if (sessions := services.session_manager.list_sessions(agent.id)) else None
            ),
        )
        for agent in agents
    ]


@router.get("/{agent_id}", response_model=AgentResponse)
def get_agent(agent_id: str, services: ServiceContainer = Depends(get_services)) -> AgentResponse:
    agent = services.repository.get_agent(agent_id)
    if agent is None:
        raise DomainError(
            code=AGENT_NOT_FOUND,
            message="Agent was not found.",
            details={"agent_id": agent_id},
        )
    sessions = services.session_manager.list_sessions(agent_id)
    default_session_id = sessions[0].id if sessions else None
    return _to_agent_response(agent, default_session_id=default_session_id)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: str, services: ServiceContainer = Depends(get_services)) -> Response:
    manager = services.get_agent_manager_for_agent(agent_id)
    await manager.delete_agent(agent_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{agent_id}/pause", response_model=AgentResponse)
def pause_agent(agent_id: str, services: ServiceContainer = Depends(get_services)) -> AgentResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    agent = manager.pause_agent(agent_id)
    sessions = services.session_manager.list_sessions(agent_id)
    default_session_id = sessions[0].id if sessions else None
    return _to_agent_response(agent, default_session_id=default_session_id)


@router.post("/{agent_id}/resume", response_model=AgentResponse)
def resume_agent(agent_id: str, services: ServiceContainer = Depends(get_services)) -> AgentResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    agent = manager.resume_agent(agent_id)
    sessions = services.session_manager.list_sessions(agent_id)
    default_session_id = sessions[0].id if sessions else None
    return _to_agent_response(agent, default_session_id=default_session_id)


@router.get("/{agent_id}/sessions", response_model=list[SessionResponse])
def list_sessions(agent_id: str, services: ServiceContainer = Depends(get_services)) -> list[SessionResponse]:
    sessions = services.session_manager.list_sessions(agent_id)
    return [SessionResponse.model_validate(session) for session in sessions]


@router.post(
    "/{agent_id}/sessions",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(agent_id: str, services: ServiceContainer = Depends(get_services)) -> SessionResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    session = await manager.create_session(agent_id)
    return SessionResponse.model_validate(session)


@router.get("/{agent_id}/sessions/{session_id}", response_model=SessionResponse)
async def get_session(
    agent_id: str,
    session_id: str,
    services: ServiceContainer = Depends(get_services),
) -> SessionResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    session = await manager.get_session(agent_id, session_id)
    return SessionResponse.model_validate(session)


@router.get("/{agent_id}/sessions/{session_id}/events")
async def get_session_events(
    agent_id: str,
    session_id: str,
    offset: int = 0,
    limit: int = 50,
    services: ServiceContainer = Depends(get_services),
) -> SessionEventsResponse:
    manager = services.get_agent_manager_for_agent(agent_id)
    adaptor_client = manager._get_adaptor_http_client(agent_id)
    try:
        result = await adaptor_client.get(
            f"/agent/sessions/{session_id}/events",
            params={"offset": offset, "limit": limit},
        )
        return SessionEventsResponse.model_validate(result)
    finally:
        await adaptor_client.close()


@router.delete("/{agent_id}/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    agent_id: str,
    session_id: str,
    services: ServiceContainer = Depends(get_services),
) -> Response:
    services.session_manager.delete_session(agent_id, session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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
    first_event = await _prefetch_first_event(event_stream)

    async def stream() -> AsyncIterator[str]:
        if first_event is None:
            return

        yield _format_sse_data(first_event)
        if first_event["event"]["type"] == "message.completed":
            return

        async for event in event_stream:
            yield _format_sse_data(event)
            if event["event"]["type"] == "message.completed":
                break

    return StreamingResponse(stream(), media_type="text/event-stream")


def _to_agent_response(agent: AgentRecord, default_session_id: str | None) -> AgentResponse:
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
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        default_session_id=default_session_id,
    )


def _format_sse_data(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event)}\n\n"


async def _prefetch_first_event(
    event_stream: AsyncIterator[dict[str, Any]],
) -> dict[str, Any] | None:
    try:
        return await anext(event_stream)
    except StopAsyncIteration:
        return None
