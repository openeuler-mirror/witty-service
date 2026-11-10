from __future__ import annotations

from fastapi import APIRouter, Depends

from witty_service.api.auth import require_bearer_auth
from witty_service.api.contract.routers import (
    agents,
    history,
    sessions,
    turns,
    workspaces,
)
from witty_service.api.errors import error_responses

router = APIRouter(
    prefix="/api",
    dependencies=[Depends(require_bearer_auth)],
)

for _module in (workspaces, sessions, turns, history, agents):
    router.include_router(_module.router, responses=error_responses(401))
