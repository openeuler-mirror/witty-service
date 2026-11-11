from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from witty_agent_server.infra.clients.opencode_v2_client import (
    OpenCodeV2Client,
    OpenCodeV2Error,
)
from witty_service.runtime.contracts import (
    EnsureSessionRequest,
    HistoryPage,
    HistoryQuery,
    ReplayQuery,
    RuntimeConnection,
    RuntimeEventEnvelope,
    RuntimeEventReplay,
    RuntimeHealth,
    RuntimeLocationReadiness,
    RuntimeModelRef,
    RuntimeRevertResult,
    RuntimeSession,
    RuntimeSessionState,
    RuntimeWorkspaceSnapshot,
    TurnAdmission,
    TurnExecutionRequest,
)
from witty_service.runtime.errors import (
    RuntimeOperationNotImplementedError,
    RuntimeUnavailableError,
)


class OpenCodeRuntimeAdapter:
    """v2 adapter 骨架：本阶段只绑定连接、健康检查与 session 建立。"""

    def __init__(
        self,
        *,
        client_factory: Callable[[str], OpenCodeV2Client] | None = None,
        startup_timeout: float = 30.0,
        poll_interval: float = 0.2,
    ) -> None:
        self._client_factory = client_factory or OpenCodeV2Client
        self._clients: dict[str, OpenCodeV2Client] = {}
        self._directories: dict[str, str] = {}
        self._startup_timeout = startup_timeout
        self._poll_interval = poll_interval

    async def connect(self, connection: RuntimeConnection) -> RuntimeHealth:
        workspace_id = connection.workspace.workspace_id
        client = self._client_factory(connection.base_url)
        self._clients[workspace_id] = client
        self._directories[workspace_id] = connection.workspace.runtime_directory
        deadline = time.monotonic() + self._startup_timeout
        try:
            while True:
                health = await self._probe_health(workspace_id)
                if health.ready:
                    return health
                if time.monotonic() >= deadline:
                    raise RuntimeUnavailableError(
                        workspace_id=workspace_id, detail=health.detail
                    )
                await asyncio.sleep(self._poll_interval)
        except BaseException:
            await self._close(workspace_id)
            raise

    async def disconnect(self, workspace_id: str) -> None:
        await self._close(workspace_id)

    async def health(self, workspace_id: str) -> RuntimeHealth:
        return await self._probe_health(workspace_id)

    async def inspect_workspace(self, workspace_id: str) -> RuntimeWorkspaceSnapshot:
        health = await self._probe_health(workspace_id)
        directory = self._directory(workspace_id)
        agents: tuple[str, ...] = ()
        models: tuple[RuntimeModelRef, ...] = ()
        skills: tuple[str, ...] = ()
        location_detail: str | None = None
        location_ready = health.ready
        if health.ready:
            try:
                client = self._client(workspace_id)
                location_body = await client.get_location(directory=directory)
                agents = _names(await client.list_agents(directory=directory))
                models = _models(await client.list_models(directory=directory))
                skills = _names(await client.list_skills(directory=directory))
                if location_body.get("directory") != directory:
                    location_ready = False
                    location_detail = "location is not bound to the workspace directory"
                elif not agents or not models:
                    location_ready = False
                    location_detail = "location catalog is not ready"
            except (OpenCodeV2Error, httpx.HTTPError) as exc:
                location_ready = False
                location_detail = str(exc)
        readiness = RuntimeLocationReadiness(
            workspace_id=workspace_id,
            ready=location_ready,
            agents=agents,
            models=models,
            skills=skills,
            detail=location_detail,
        )
        return RuntimeWorkspaceSnapshot(
            workspace_id=workspace_id,
            health=health,
            location=readiness,
        )

    async def ensure_session(self, request: EnsureSessionRequest) -> RuntimeSession:
        workspace_id = request.workspace.workspace_id
        directory = self._require_directory(
            workspace_id, request.workspace.runtime_directory
        )
        client = self._client(workspace_id)
        if request.runtime_session_id is not None:
            existing = await self._get_session(
                client, workspace_id, request.runtime_session_id
            )
            if existing is not None:
                return RuntimeSession(
                    workspace_id=workspace_id,
                    session_id=request.session_id,
                    runtime_session_id=request.runtime_session_id,
                )
        try:
            created = await client.create_session(directory=directory)
        except (OpenCodeV2Error, httpx.HTTPError) as exc:
            raise RuntimeUnavailableError(
                workspace_id=workspace_id, detail="failed to create runtime session"
            ) from exc
        runtime_session_id = created.get("id")
        if not isinstance(runtime_session_id, str) or not runtime_session_id:
            raise RuntimeUnavailableError(
                workspace_id=workspace_id,
                detail="runtime session response did not contain an id",
            )
        return RuntimeSession(
            workspace_id=workspace_id,
            session_id=request.session_id,
            runtime_session_id=runtime_session_id,
        )

    async def prepare_turn(self, request: TurnExecutionRequest) -> None:
        raise RuntimeOperationNotImplementedError(operation="prepare_turn")

    async def admit_turn(self, request: TurnExecutionRequest) -> TurnAdmission:
        raise RuntimeOperationNotImplementedError(operation="admit_turn")

    async def subscribe(self, workspace_id: str) -> AsyncIterator[RuntimeEventEnvelope]:
        raise RuntimeOperationNotImplementedError(operation="subscribe")
        yield  # pragma: no cover - 保持异步生成器签名

    async def replay_events(self, query: ReplayQuery) -> RuntimeEventReplay:
        raise RuntimeOperationNotImplementedError(operation="replay_events")

    async def read_history(self, query: HistoryQuery) -> HistoryPage:
        raise RuntimeOperationNotImplementedError(operation="read_history")

    async def interrupt_session(
        self, *, workspace_id: str, runtime_session_id: str
    ) -> None:
        raise RuntimeOperationNotImplementedError(operation="interrupt_session")

    async def stage_revert(
        self,
        *,
        workspace_id: str,
        runtime_session_id: str,
        runtime_message_id: str,
    ) -> RuntimeRevertResult:
        raise RuntimeOperationNotImplementedError(operation="stage_revert")

    async def commit_revert(
        self, *, workspace_id: str, runtime_session_id: str
    ) -> None:
        raise RuntimeOperationNotImplementedError(operation="commit_revert")

    async def clear_revert(self, *, workspace_id: str, runtime_session_id: str) -> None:
        raise RuntimeOperationNotImplementedError(operation="clear_revert")

    async def delete_session(
        self, *, workspace_id: str, runtime_session_id: str
    ) -> None:
        raise RuntimeOperationNotImplementedError(operation="delete_session")

    async def inspect_session(
        self, *, workspace_id: str, runtime_session_id: str
    ) -> RuntimeSessionState:
        raise RuntimeOperationNotImplementedError(operation="inspect_session")

    def _client(self, workspace_id: str) -> OpenCodeV2Client:
        client = self._clients.get(workspace_id)
        if client is None:
            raise RuntimeUnavailableError(
                workspace_id=workspace_id, detail="workspace runtime is not connected"
            )
        return client

    def _directory(self, workspace_id: str) -> str:
        directory = self._directories.get(workspace_id)
        if directory is None:
            raise RuntimeUnavailableError(
                workspace_id=workspace_id, detail="workspace runtime is not connected"
            )
        return directory

    def _require_directory(self, workspace_id: str, directory: str) -> str:
        connected = self._directory(workspace_id)
        if connected != directory:
            raise RuntimeUnavailableError(
                workspace_id=workspace_id,
                detail="runtime session directory does not match the connected workspace",
            )
        return connected

    async def _probe_health(self, workspace_id: str) -> RuntimeHealth:
        client = self._clients.get(workspace_id)
        if client is None:
            return RuntimeHealth(
                workspace_id=workspace_id,
                ready=False,
                detail="workspace runtime is not connected",
            )
        try:
            ready = await client.healthy()
        except (OpenCodeV2Error, httpx.HTTPError) as exc:
            return RuntimeHealth(
                workspace_id=workspace_id, ready=False, detail=str(exc)
            )
        return RuntimeHealth(
            workspace_id=workspace_id,
            ready=ready,
            detail=None if ready else "runtime health endpoint is not ready",
        )

    async def _get_session(
        self, client: OpenCodeV2Client, workspace_id: str, runtime_session_id: str
    ) -> dict[str, Any] | None:
        try:
            return await client.get_session(runtime_session_id)
        except OpenCodeV2Error as exc:
            if exc.status_code == 404:
                return None
            raise RuntimeUnavailableError(
                workspace_id=workspace_id, detail="failed to inspect runtime session"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeUnavailableError(
                workspace_id=workspace_id, detail="failed to inspect runtime session"
            ) from exc

    async def _close(self, workspace_id: str) -> None:
        self._directories.pop(workspace_id, None)
        client = self._clients.pop(workspace_id, None)
        if client is not None:
            await client.aclose()


def _names(entries: list[dict[str, Any]]) -> tuple[str, ...]:
    names = []
    for entry in entries:
        name = entry.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return tuple(sorted(names))


def _models(entries: list[dict[str, Any]]) -> tuple[RuntimeModelRef, ...]:
    models = []
    for entry in entries:
        model_id = entry.get("id")
        provider_id = entry.get("providerID")
        if isinstance(model_id, str) and isinstance(provider_id, str):
            models.append(RuntimeModelRef(provider_id=provider_id, model_id=model_id))
    return tuple(sorted(models, key=lambda model: (model.provider_id, model.model_id)))
