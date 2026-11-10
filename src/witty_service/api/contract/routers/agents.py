from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from witty_service.api.contract.dto.agent import (
    AgentListResponse,
    AgentResponse,
    CreateAgentRequest,
    UpdateAgentRequest,
)
from witty_service.api.contract.dto.common import AgentKind
from witty_service.api.contract.routers._stub import not_implemented
from witty_service.api.errors import error_responses

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("", response_model=AgentListResponse, responses=error_responses(400))
def list_agents(
    kind: AgentKind | None = None,
    include_archived: bool = False,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AgentListResponse:
    not_implemented()


@router.get("/{agent_id}", response_model=AgentResponse, responses=error_responses(404))
def get_agent(agent_id: str) -> AgentResponse:
    not_implemented()


@router.post(
    "",
    response_model=AgentResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(400, 409),
)
def create_agent(payload: CreateAgentRequest) -> AgentResponse:
    not_implemented()


@router.put(
    "/{agent_id}",
    response_model=AgentResponse,
    responses=error_responses(400, 404, 409),
)
def update_agent(agent_id: str, payload: UpdateAgentRequest) -> AgentResponse:
    not_implemented()


@router.post(
    "/{agent_id}/archive",
    response_model=AgentResponse,
    responses=error_responses(404, 409),
)
def archive_agent(agent_id: str) -> AgentResponse:
    not_implemented()
