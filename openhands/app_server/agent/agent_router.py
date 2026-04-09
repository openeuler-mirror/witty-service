"""Agent Router for Agent Middleware Service.

This module defines the API endpoints for Agent management as specified
in Section 8 of the design specification.

API Endpoints:
- POST   /api/v1/agents                   - Create agent
- GET    /api/v1/agents                   - List agents
- GET    /api/v1/agents/{agent_id}        - Get agent details
- PATCH  /api/v1/agents/{agent_id}        - Update agent config
- DELETE /api/v1/agents/{agent_id}        - Delete agent
- POST   /api/v1/agents/{agent_id}/pause  - Pause agent
- POST   /api/v1/agents/{agent_id}/resume - Resume agent
- GET    /api/v1/agents/{agent_id}/sessions        - List sessions
- POST   /api/v1/agents/{agent_id}/sessions        - Create session
- GET    /api/v1/agents/{agent_id}/sessions/{session_id} - Get session
- DELETE /api/v1/agents/{agent_id}/sessions/{session_id} - Delete session
- POST   /api/v1/agents/{agent_id}/messages - Send message
- GET    /api/v1/agents/{agent_id}/ws      - WebSocket subscription
"""

import logging
import json
from typing import TYPE_CHECKING, AsyncIterator

from fastapi import APIRouter, HTTPException, status, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import StreamingResponse

from openhands.app_server.agent.errors import (
    AgentNotFoundError,
    SessionNotFoundError,
    AgentNotRunningError,
    AgentPausedError,
)
from openhands.app_server.agent.models import (
    AgentInfo,
    AgentStatus,
    CreateAgentRequest,
    ErrorResponse,
    SendMessageRequest,
    SessionInfo,
    UpdateAgentRequest,
)

if TYPE_CHECKING:
    from openhands.app_server.agent.agent_manager import AgentManager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])


def get_agent_manager() -> "AgentManager":
    """Get singleton AgentManager instance."""
    from openhands.app_server.agent.agent_manager import AgentManager
    return AgentManager.get_instance()


def reset_agent_singleton() -> None:
    """Reset singleton instances. For testing purposes."""
    from openhands.app_server.agent.agent_manager import AgentManager
    from openhands.app_server.agent.adapter_client import reset_adapter_client_pool
    AgentManager.reset_instance()
    reset_adapter_client_pool()


@router.post(
    "",
    response_model=AgentInfo,
    status_code=status.HTTP_201_CREATED,
    responses={
        422: {"model": ErrorResponse, "description": "Validation Error"},
    },
)
async def create_agent(request: CreateAgentRequest) -> AgentInfo:
    """Create a new agent (async provisioning).

    Returns immediately with ``status=CREATING``. Provisioning continues in the
    background; poll ``GET /agents/{id}`` for ``status``, ``creation_log``, and
    ``creation_error``.
    """
    if not request.name or not request.name.strip():
        raise HTTPException(status_code=422, detail={"error": {"code": "VALIDATION_ERROR", "message": "Validation error: Agent name cannot be empty", "details": {"field": "name"}}})
    if not request.template:
        raise HTTPException(status_code=422, detail={"error": {"code": "VALIDATION_ERROR", "message": "Validation error: Agent template is required", "details": {"field": "template"}}})
    manager = get_agent_manager()
    return await manager.create_agent(request)


@router.get(
    "",
    response_model=list[AgentInfo],
)
async def list_agents() -> list[AgentInfo]:
    """List all agents.

    Returns a list of all agents in the system.
    """
    manager = get_agent_manager()
    return await manager.list_agents()


@router.get(
    "/{agent_id}",
    response_model=AgentInfo,
    responses={
        404: {"model": ErrorResponse, "description": "Agent Not Found"},
    },
)
async def get_agent(agent_id: str) -> AgentInfo:
    """Get agent details by ID.

    Returns detailed information about a specific agent.
    """
    manager = get_agent_manager()
    agent = await manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=AgentNotFoundError(agent_id).to_dict())
    return agent


@router.patch(
    "/{agent_id}",
    response_model=AgentInfo,
    responses={
        404: {"model": ErrorResponse, "description": "Agent Not Found"},
        400: {"model": ErrorResponse, "description": "Agent Not Running"},
    },
)
async def update_agent(
    agent_id: str,
    request: UpdateAgentRequest,
) -> AgentInfo:
    """Update agent configuration.

    Allows updating agent name, model override, and idle timeout.
    """
    manager = get_agent_manager()
    agent = await manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=AgentNotFoundError(agent_id).to_dict())
    if agent.status not in (AgentStatus.RUNNING, AgentStatus.PAUSED):
        raise HTTPException(status_code=400, detail=AgentNotRunningError(agent_id, agent.status.value).to_dict())
    updated = await manager.update_agent(agent_id, request)
    if not updated:
        raise HTTPException(status_code=404, detail=AgentNotFoundError(agent_id).to_dict())
    return updated


@router.delete(
    "/{agent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"model": ErrorResponse, "description": "Agent Not Found"},
    },
)
async def delete_agent(agent_id: str) -> None:
    """Delete an agent.

    Removes the agent and all associated sessions. The sandbox
    will be stopped and workspace cleaned up.
    """
    manager = get_agent_manager()
    agent = await manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=AgentNotFoundError(agent_id).to_dict())
    await manager.delete_agent(agent_id)


@router.post(
    "/{agent_id}/pause",
    response_model=AgentInfo,
    responses={
        404: {"model": ErrorResponse, "description": "Agent Not Found"},
        400: {"model": ErrorResponse, "description": "Agent Not Running"},
    },
)
async def pause_agent(agent_id: str) -> AgentInfo:
    """Pause an agent.

    Stops the sandbox and saves agent state. The agent can be
    resumed later to continue from where it left off.
    """
    manager = get_agent_manager()
    agent = await manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=AgentNotFoundError(agent_id).to_dict())
    if agent.status != AgentStatus.RUNNING:
        raise HTTPException(status_code=400, detail=AgentNotRunningError(agent_id, agent.status.value).to_dict())
    paused = await manager.pause_agent(agent_id)
    if not paused:
        raise HTTPException(status_code=400, detail=AgentNotRunningError(agent_id, agent.status.value).to_dict())
    return paused


@router.post(
    "/{agent_id}/resume",
    response_model=AgentInfo,
    responses={
        404: {"model": ErrorResponse, "description": "Agent Not Found"},
        400: {"model": ErrorResponse, "description": "Agent Not Paused"},
    },
)
async def resume_agent(agent_id: str) -> AgentInfo:
    """Resume a paused agent.

    Restarts the sandbox and restores agent state from workspace.
    """
    manager = get_agent_manager()
    agent = await manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=AgentNotFoundError(agent_id).to_dict())
    if agent.status != AgentStatus.PAUSED:
        raise HTTPException(status_code=400, detail=AgentPausedError(agent_id).to_dict())
    resumed = await manager.resume_agent(agent_id)
    if not resumed:
        raise HTTPException(status_code=400, detail=AgentPausedError(agent_id).to_dict())
    return resumed


@router.get(
    "/{agent_id}/sessions",
    response_model=list[SessionInfo],
    responses={
        404: {"model": ErrorResponse, "description": "Agent Not Found"},
    },
)
async def list_sessions(agent_id: str) -> list[SessionInfo]:
    """List all sessions for an agent.

    Returns all sessions associated with the specified agent.
    """
    manager = get_agent_manager()
    agent = await manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=AgentNotFoundError(agent_id).to_dict())
    return await manager.list_sessions(agent_id)


@router.post(
    "/{agent_id}/sessions",
    response_model=SessionInfo,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"model": ErrorResponse, "description": "Agent Not Found"},
    },
)
async def create_session(agent_id: str, request: dict = None) -> SessionInfo:
    """Create a new session for an agent.

    Creates a new independent conversation context.
    """
    session_id = request.get("session_id") if request else None
    manager = get_agent_manager()
    agent = await manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=AgentNotFoundError(agent_id).to_dict())
    return await manager.create_session(agent_id, session_id)


@router.get(
    "/{agent_id}/sessions/{session_id}",
    response_model=SessionInfo,
    responses={
        404: {"model": ErrorResponse, "description": "Agent or Session Not Found"},
    },
)
async def get_session(agent_id: str, session_id: str) -> SessionInfo:
    """Get session details.

    Returns detailed information about a specific session.
    """
    manager = get_agent_manager()
    agent = await manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=AgentNotFoundError(agent_id).to_dict())
    session = await manager.get_session(agent_id, session_id)
    if not session:
        raise HTTPException(status_code=404, detail=SessionNotFoundError(session_id).to_dict())
    return session


@router.delete(
    "/{agent_id}/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {"model": ErrorResponse, "description": "Agent or Session Not Found"},
    },
)
async def delete_session(agent_id: str, session_id: str) -> None:
    """Delete a session.

    Removes the session and its associated messages.
    """
    manager = get_agent_manager()
    agent = await manager.get_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=AgentNotFoundError(agent_id).to_dict())
    session = await manager.get_session(agent_id, session_id)
    if not session:
        raise HTTPException(status_code=404, detail=SessionNotFoundError(session_id).to_dict())
    await manager.delete_session(agent_id, session_id)


@router.post(
    "/{agent_id}/messages",
    responses={
        200: {"description": "Stream of AgentEvent"},
        404: {"model": ErrorResponse, "description": "Agent or Session Not Found"},
        400: {"model": ErrorResponse, "description": "Agent Not Running"},
    },
)
async def send_message(
    agent_id: str,
    request: SendMessageRequest,
) -> StreamingResponse:
    """Send a message to an agent.

    Sends a message to the agent and returns a streaming response
    with events (thinking, message, tool_use, done).

    The response is a Server-Sent Events (SSE) stream.
    """

    async def event_generator() -> AsyncIterator[bytes]:
        manager = get_agent_manager()
        agent = await manager.get_agent(agent_id)
        if not agent:
            yield f"event: error\ndata: {json.dumps(AgentNotFoundError(agent_id).to_dict(), ensure_ascii=False)}\n\n".encode()
            return
        session = await manager.get_session(agent_id, request.session_id)
        if not session:
            yield f"event: error\ndata: {json.dumps(SessionNotFoundError(request.session_id).to_dict(), ensure_ascii=False)}\n\n".encode()
            return
        async for event in manager.send_message(agent_id, request.session_id, request.content):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/{agent_id}/sessions/{session_id}/messages",
    responses={
        200: {"description": "Stream of AgentEvent"},
        404: {"model": ErrorResponse, "description": "Agent or Session Not Found"},
        400: {"model": ErrorResponse, "description": "Agent Not Running"},
    },
)
async def send_message_by_session(
    agent_id: str,
    session_id: str,
    request: SendMessageRequest,
) -> StreamingResponse:
    req = request.model_copy(update={"session_id": session_id})
    return await send_message(agent_id, req)


@router.websocket("/{agent_id}/ws")
async def websocket_endpoint(websocket: WebSocket, agent_id: str, session_id: str = Query(...)):
    """WebSocket subscription for agent events.

    Establishes a WebSocket connection for real-time agent event streaming.
    Events are pushed to the client as they occur.
    """
    await websocket.accept()

    try:
        manager = get_agent_manager()
        session = await manager.get_session(agent_id, session_id)
        if not session:
            await websocket.send_json(SessionNotFoundError(session_id).to_dict())
            await websocket.close()
            return
        while True:
            payload = await websocket.receive_json()
            if payload.get("type") != "message":
                continue
            message = payload.get("content", "")
            if not message:
                continue
            request = SendMessageRequest(session_id=session_id, content=message)
            async for event in manager.send_message_ws(agent_id, request.session_id, request.content):
                await websocket.send_json(event)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for agent {agent_id}")
    except Exception as e:
        logger.error(f"WebSocket error for agent {agent_id}: {e}")
        await websocket.send_json({
            "type": "error",
            "content": str(e),
        })
        await websocket.close()
