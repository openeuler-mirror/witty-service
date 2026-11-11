from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import uuid4

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
    RuntimeEmptyPayload,
    RuntimeErrorPayload,
    RuntimeEventEnvelope,
    RuntimeEventReplay,
    RuntimeEventType,
    RuntimeHealth,
    RuntimeHistoryMessage,
    RuntimeLocationReadiness,
    RuntimeLocationVerification,
    RuntimeMessageCompletedPayload,
    RuntimeMessageDeltaPayload,
    RuntimeMessageKind,
    RuntimeModelRef,
    RuntimeReasoningPart,
    RuntimeRevertResult,
    RuntimeSession,
    RuntimeSessionState,
    RuntimeTextPart,
    RuntimeWorkspaceSnapshot,
    TurnAdmission,
    TurnExecutionRequest,
)
from witty_service.runtime.errors import (
    HistoryCursorSessionMismatchError,
    InvalidHistoryCursorError,
    RuntimeAdmissionConflictError,
    RuntimeContractValidationError,
    RuntimeOperationNotImplementedError,
    RuntimeSessionNotFoundError,
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
        stale = self._clients.pop(workspace_id, None)
        if stale is not None:
            await stale.aclose()
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

    async def verify_location(
        self, request: RuntimeLocationVerification
    ) -> RuntimeLocationReadiness:
        """预热目标 location 直到出现 ``step.started``，并核对可见性。"""
        deadline = time.monotonic() + request.timeout
        while True:
            snapshot = await self.inspect_workspace(request.workspace_id)
            location = snapshot.location
            missing = _missing_requirements(request, location)
            if location.ready and missing is None:
                return await self._warm_location(request, location, deadline)
            if time.monotonic() >= deadline:
                return _readiness(location, detail=missing or location.detail)
            await asyncio.sleep(self._poll_interval)

    async def _warm_location(
        self,
        request: RuntimeLocationVerification,
        location: RuntimeLocationReadiness,
        deadline: float,
    ) -> RuntimeLocationReadiness:
        if not location.agents or not location.models:
            return _readiness(
                location, detail="location catalog has no agents or models"
            )
        agent = request.agents[0] if request.agents else location.agents[0]
        model = request.models[0] if request.models else location.models[0]
        client = self._client(request.workspace_id)
        workspace_id = request.workspace_id
        try:
            created = await client.create_session(
                directory=self._directory(workspace_id),
                agent=agent,
                model={"providerID": model.provider_id, "id": model.model_id},
            )
        except (OpenCodeV2Error, httpx.HTTPError) as exc:
            return _readiness(location, detail=f"failed to open warm-up session: {exc}")
        runtime_session_id = created.get("id")
        if not isinstance(runtime_session_id, str) or not runtime_session_id:
            return _readiness(location, detail="warm-up session has no id")
        try:
            await client.prompt(
                runtime_session_id,
                message_id=f"warmup_{uuid4().hex}",
                text="warm up",
            )
            while True:
                events, _ = await client.history(
                    runtime_session_id, after_seq=0, limit=100
                )
                if any(_is_turn_started(workspace_id, raw) for raw in events):
                    return _readiness(location, ready=True)
                if time.monotonic() >= deadline:
                    return _readiness(
                        location, detail="warm-up turn did not start at the location"
                    )
                await asyncio.sleep(self._poll_interval)
        except (OpenCodeV2Error, httpx.HTTPError) as exc:
            return _readiness(location, detail=f"warm-up turn failed: {exc}")

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
        workspace_id = request.workspace_id
        if request.file_parts:
            raise RuntimeContractValidationError(
                field="parts", reason="file parts are not supported by the v2 adapter"
            )
        snapshot = await self.inspect_workspace(workspace_id)
        if not snapshot.location.ready:
            raise RuntimeUnavailableError(
                workspace_id=workspace_id,
                detail=snapshot.location.detail or "location is not ready",
            )
        if request.agent_internal_name not in snapshot.location.agents:
            raise RuntimeContractValidationError(
                field="agent_internal_name",
                reason="agent is not available at the runtime location",
            )
        known_models = {
            (model.provider_id, model.model_id) for model in snapshot.location.models
        }
        requested_model = (request.model.provider_id, request.model.model_id)
        if requested_model not in known_models:
            raise RuntimeContractValidationError(
                field="model", reason="model is not available at the runtime location"
            )
        client = self._client(workspace_id)
        session = await self._get_session(
            client, workspace_id, request.runtime_session_id
        )
        if session is None:
            raise RuntimeSessionNotFoundError(
                runtime_session_id=request.runtime_session_id,
            )
        current_model = session.get("model")
        current_pair = (
            (
                current_model.get("providerID"),
                current_model.get("id"),
            )
            if isinstance(current_model, dict)
            else (None, None)
        )
        try:
            if session.get("agent") != request.agent_internal_name:
                await client.switch_agent(
                    request.runtime_session_id, agent=request.agent_internal_name
                )
            if current_pair != requested_model:
                await client.switch_model(
                    request.runtime_session_id,
                    model={
                        "providerID": request.model.provider_id,
                        "id": request.model.model_id,
                    },
                )
        except OpenCodeV2Error as exc:
            if exc.status_code == 404:
                raise RuntimeSessionNotFoundError(
                    runtime_session_id=request.runtime_session_id,
                ) from exc
            raise RuntimeUnavailableError(
                workspace_id=workspace_id, detail="failed to apply session options"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeUnavailableError(
                workspace_id=workspace_id, detail="failed to apply session options"
            ) from exc

    async def admit_turn(self, request: TurnExecutionRequest) -> TurnAdmission:
        await self.prepare_turn(request)
        client = self._client(request.workspace_id)
        try:
            admitted = await client.prompt(
                request.runtime_session_id,
                message_id=request.message_id,
                text=request.text,
                resume=request.resume,
            )
        except OpenCodeV2Error as exc:
            if exc.status_code == 404:
                raise RuntimeSessionNotFoundError(
                    runtime_session_id=request.runtime_session_id,
                ) from exc
            if exc.status_code == 409:
                raise RuntimeAdmissionConflictError(
                    runtime_session_id=request.runtime_session_id,
                    message_id=request.message_id,
                ) from exc
            raise RuntimeUnavailableError(
                workspace_id=request.workspace_id, detail="failed to admit turn"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeUnavailableError(
                workspace_id=request.workspace_id, detail="failed to admit turn"
            ) from exc
        return TurnAdmission(
            message_id=_text_or(admitted.get("id"), request.message_id),
            runtime_session_id=_text_or(
                admitted.get("sessionID"), request.runtime_session_id
            ),
            admitted_seq=_int_or_zero(admitted.get("admittedSeq")),
            delivery=_text_or(admitted.get("delivery"), "steer"),
            admitted_at=_int_or_zero(admitted.get("timeCreated")),
        )

    async def subscribe(self, workspace_id: str) -> AsyncIterator[RuntimeEventEnvelope]:
        directory = self._directory(workspace_id)
        client = self._client(workspace_id)
        try:
            async for payload in client.stream_events():
                location = payload.get("location")
                if not isinstance(location, dict):
                    continue
                if location.get("directory") != directory:
                    continue
                envelope = _map_event(workspace_id, payload)
                if envelope is not None:
                    yield envelope
        except (OpenCodeV2Error, httpx.HTTPError) as exc:
            raise RuntimeUnavailableError(
                workspace_id=workspace_id, detail="runtime event stream failed"
            ) from exc

    async def replay_events(self, query: ReplayQuery) -> RuntimeEventReplay:
        client = self._client(query.workspace_id)
        try:
            raw_events, has_more = await client.history(
                query.runtime_session_id, after_seq=query.after_seq, limit=query.limit
            )
        except OpenCodeV2Error as exc:
            if exc.status_code == 404:
                raise RuntimeSessionNotFoundError(
                    runtime_session_id=query.runtime_session_id,
                ) from exc
            if exc.status_code == 400:
                raise RuntimeContractValidationError(
                    field="after_seq", reason="runtime rejected the replay cursor"
                ) from exc
            raise RuntimeUnavailableError(
                workspace_id=query.workspace_id, detail="failed to replay events"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeUnavailableError(
                workspace_id=query.workspace_id, detail="failed to replay events"
            ) from exc
        events = tuple(
            envelope
            for envelope in (_map_event(query.workspace_id, raw) for raw in raw_events)
            if envelope is not None
        )
        return RuntimeEventReplay(events=events, has_more=has_more)

    async def read_history(self, query: HistoryQuery) -> HistoryPage:
        client = self._client(query.workspace_id)
        cursor = (
            _decode_cursor(query.runtime_session_id, query.cursor)
            if query.cursor is not None
            else None
        )
        try:
            raw_messages, cursors = await client.messages(
                query.runtime_session_id, limit=query.limit, cursor=cursor
            )
        except OpenCodeV2Error as exc:
            if exc.status_code == 404:
                raise RuntimeSessionNotFoundError(
                    runtime_session_id=query.runtime_session_id,
                ) from exc
            if exc.status_code == 400:
                raise InvalidHistoryCursorError(
                    runtime_session_id=query.runtime_session_id,
                ) from exc
            raise RuntimeUnavailableError(
                workspace_id=query.workspace_id, detail="failed to read history"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeUnavailableError(
                workspace_id=query.workspace_id, detail="failed to read history"
            ) from exc
        items = tuple(
            message
            for message in (_map_message(raw) for raw in raw_messages)
            if message is not None
        )
        return HistoryPage(
            items=items,
            next_cursor=_encode_cursor(query.runtime_session_id, cursors.get("next")),
            previous_cursor=_encode_cursor(
                query.runtime_session_id, cursors.get("previous")
            ),
        )

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


def _text_or(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def _encode_cursor(session_id: str, cursor: str | None) -> str | None:
    return f"{session_id}:{cursor}" if cursor else None


def _decode_cursor(session_id: str, cursor: str) -> str:
    prefix, separator, upstream = cursor.partition(":")
    if not separator or not upstream:
        raise InvalidHistoryCursorError(runtime_session_id=session_id)
    if prefix != session_id:
        raise HistoryCursorSessionMismatchError(runtime_session_id=session_id)
    return upstream


_EPHEMERAL_EVENT_TYPES = frozenset(
    {
        RuntimeEventType.MESSAGE_DELTA,
        RuntimeEventType.THINKING_DELTA,
        RuntimeEventType.TOOL_CALL_DELTA,
        RuntimeEventType.ARTIFACT_DELTA,
        RuntimeEventType.SESSION_ERROR,
        RuntimeEventType.SESSION_IDLE,
    }
)


def _map_event(
    workspace_id: str, payload: dict[str, Any]
) -> RuntimeEventEnvelope | None:
    event_type = payload.get("type")
    data = payload.get("data")
    durable = payload.get("durable")
    if not isinstance(event_type, str) or not isinstance(data, dict):
        return None
    runtime_session_id = data.get("sessionID")
    if not isinstance(runtime_session_id, str) or not runtime_session_id:
        return None
    seq = durable.get("seq") if isinstance(durable, dict) else None
    if seq is not None and (not isinstance(seq, int) or seq < 0):
        return None
    mapped = _event_payload(event_type, data)
    if mapped is None:
        return None
    kind, body = mapped
    if seq is None and kind not in _EPHEMERAL_EVENT_TYPES:
        return None
    occurred_at = _int_or_zero(data.get("timestamp"))
    message_id = data.get("assistantMessageID")
    return RuntimeEventEnvelope(
        workspace_id=workspace_id,
        runtime_session_id=runtime_session_id,
        event_id=_text_or(payload.get("id"), f"evt_{runtime_session_id}_{occurred_at}"),
        type=kind,
        durable_seq=seq,
        occurred_at=occurred_at,
        payload=body,
        runtime_message_id=message_id
        if isinstance(message_id, str) and message_id
        else None,
    )


def _event_payload(
    event_type: str, data: dict[str, Any]
) -> tuple[RuntimeEventType, Any] | None:
    if event_type == "session.next.step.started":
        return RuntimeEventType.TURN_STARTED, RuntimeEmptyPayload()
    if event_type == "session.next.text.started":
        return RuntimeEventType.MESSAGE_STARTED, RuntimeEmptyPayload()
    if event_type == "session.next.text.delta":
        delta = data.get("delta")
        if not isinstance(delta, str):
            return None
        return RuntimeEventType.MESSAGE_DELTA, RuntimeMessageDeltaPayload(delta=delta)
    if event_type == "session.next.text.ended":
        return RuntimeEventType.MESSAGE_COMPLETED, RuntimeMessageCompletedPayload()
    if event_type == "session.next.step.ended":
        return RuntimeEventType.TURN_COMPLETED, RuntimeEmptyPayload()
    if event_type == "session.next.step.failed":
        raw_error = data.get("error")
        error = raw_error if isinstance(raw_error, dict) else {}
        return RuntimeEventType.TURN_FAILED, RuntimeErrorPayload(
            code=_text_or(error.get("code"), "step_failed"),
            message=_text_or(error.get("message"), "runtime step failed"),
            retryable=error.get("retryable") is True,
        )
    if event_type == "session.error":
        raw_error = data.get("error")
        error = raw_error if isinstance(raw_error, dict) else data
        return RuntimeEventType.SESSION_ERROR, RuntimeErrorPayload(
            code=_text_or(error.get("code"), "session_error"),
            message=_text_or(error.get("message"), "runtime session error"),
            retryable=error.get("retryable") is True,
        )
    if event_type == "session.idle":
        return RuntimeEventType.SESSION_IDLE, RuntimeEmptyPayload()
    return None


def _map_message(raw: dict[str, Any]) -> RuntimeHistoryMessage | None:
    message_id = raw.get("id")
    if not isinstance(message_id, str) or not message_id:
        return None
    created = raw.get("time")
    created_at = _int_or_zero(
        created.get("created") if isinstance(created, dict) else 0
    )
    kind = raw.get("type")
    if kind == "user":
        parts = _text_parts(raw)
        message_kind = RuntimeMessageKind.USER
    elif kind == "assistant":
        parts = _assistant_parts(raw)
        message_kind = RuntimeMessageKind.ASSISTANT
    elif kind == "agent-switched":
        parts = ()
        message_kind = RuntimeMessageKind.AGENT_SWITCHED
    elif kind == "model-switched":
        parts = ()
        message_kind = RuntimeMessageKind.MODEL_SWITCHED
    else:
        return None
    finish = raw.get("finish")
    return RuntimeHistoryMessage(
        message_id=message_id,
        kind=message_kind,
        created_at=created_at,
        parts=parts,
        finish=finish if isinstance(finish, str) and finish else None,
    )


def _text_parts(raw: dict[str, Any]) -> tuple[RuntimeTextPart, ...]:
    text = raw.get("text")
    if isinstance(text, str) and text:
        return (RuntimeTextPart(text=text),)
    return _assistant_parts(raw)


def _assistant_parts(raw: dict[str, Any]) -> tuple[Any, ...]:
    content = raw.get("content")
    if not isinstance(content, list):
        return ()
    parts: list[Any] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if part.get("type") == "text" and isinstance(text, str) and text:
            parts.append(RuntimeTextPart(text=text))
        elif part.get("type") == "reasoning" and isinstance(text, str) and text:
            parts.append(RuntimeReasoningPart(text=text))
    return tuple(parts)


def _names(entries: list[dict[str, Any]]) -> tuple[str, ...]:
    names = []
    for entry in entries:
        name = entry.get("id") or entry.get("name")
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


def _missing_requirements(
    request: RuntimeLocationVerification, location: RuntimeLocationReadiness
) -> str | None:
    missing_agents = [name for name in request.agents if name not in location.agents]
    if missing_agents:
        return f"agents are not visible at the location: {', '.join(missing_agents)}"
    available = {(model.provider_id, model.model_id) for model in location.models}
    missing_models = [
        f"{model.provider_id}/{model.model_id}"
        for model in request.models
        if (model.provider_id, model.model_id) not in available
    ]
    if missing_models:
        return f"models are not visible at the location: {', '.join(missing_models)}"
    missing_skills = [name for name in request.skills if name not in location.skills]
    if missing_skills:
        return f"skills are not visible at the location: {', '.join(missing_skills)}"
    return None


def _readiness(
    location: RuntimeLocationReadiness,
    *,
    ready: bool = False,
    detail: str | None = None,
) -> RuntimeLocationReadiness:
    return RuntimeLocationReadiness(
        workspace_id=location.workspace_id,
        ready=ready,
        agents=location.agents,
        models=location.models,
        skills=location.skills,
        detail=None if ready else detail,
    )


def _is_turn_started(workspace_id: str, raw: dict[str, Any]) -> bool:
    envelope = _map_event(workspace_id, raw)
    return envelope is not None and envelope.type is RuntimeEventType.TURN_STARTED
