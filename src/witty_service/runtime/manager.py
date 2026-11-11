from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from witty_service.runtime.contracts import (
    RuntimeConnection,
    RuntimeHealth,
    RuntimeLocationVerification,
    RuntimeModelRef,
    RuntimeWorkspace,
    TurnPermit,
    WorkspaceRuntimePort,
)
from witty_service.runtime.errors import (
    RuntimePublishUnavailableError,
    RuntimeUnavailableError,
)
from witty_service.runtime.projection import RuntimeProjection
from witty_service.runtime.publication import (
    PublicationPhase,
    RuntimeCredentialResolver,
    RuntimeProjectionRenderer,
    RuntimeProjectionSource,
    RuntimeProjectionStore,
    WorkspacePublicationState,
)
from witty_service.sandbox.base import (
    AdapterEndpoint,
    SandboxBackend,
    SandboxHandle,
    SandboxStatus,
)

_logger = logging.getLogger(__name__)


class RuntimePhase(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class WorkspaceRuntimeSpec:
    """启动一个 Workspace runtime 所需的全部外部输入。"""

    workspace: RuntimeWorkspace
    host_directory: str
    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    preferred_port: int | None = None
    stderr_log_path: str | None = None

    def __post_init__(self) -> None:
        if not self.host_directory.strip():
            raise ValueError("host_directory must not be empty")
        if not self.command:
            raise ValueError("command must not be empty")


@dataclass(frozen=True, slots=True)
class WorkspaceRuntimeState:
    workspace_id: str
    phase: RuntimePhase
    base_url: str | None = None
    sandbox_id: str | None = None
    started_at: float | None = None
    last_error: str | None = None


@dataclass(slots=True)
class _ActiveTurn:
    turn_id: str
    runtime_session_id: str | None = None
    terminal: bool = False


def _drained_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


@dataclass(slots=True)
class _ManagedWorkspace:
    spec: WorkspaceRuntimeSpec
    phase: RuntimePhase = RuntimePhase.STOPPED
    base_url: str | None = None
    sandbox_id: str | None = None
    started_at: float | None = None
    last_error: str | None = None
    handle: SandboxHandle | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    publication: PublicationPhase = PublicationPhase.IDLE
    published_hash: str | None = None
    published_loaded: bool = False
    pending_hash: str | None = None
    publish_error: str | None = None
    publish_task: asyncio.Task[None] | None = None
    active_turns: dict[str, _ActiveTurn] = field(default_factory=dict)
    drained: asyncio.Event = field(default_factory=_drained_event)
    permit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_activity: float = 0.0

    def snapshot(self) -> WorkspaceRuntimeState:
        return WorkspaceRuntimeState(
            workspace_id=self.spec.workspace.workspace_id,
            phase=self.phase,
            base_url=self.base_url,
            sandbox_id=self.sandbox_id,
            started_at=self.started_at,
            last_error=self.last_error,
        )

    def publication_state(self) -> WorkspacePublicationState:
        return WorkspacePublicationState(
            workspace_id=self.spec.workspace.workspace_id,
            phase=self.publication,
            published_projection_hash=self.published_hash,
            pending_projection_hash=self.pending_hash,
            active_turns=len(self.active_turns),
            last_error=self.publish_error,
        )

    def reset(self, phase: RuntimePhase, *, keep_error: bool = False) -> None:
        self.phase = phase
        self.base_url = None
        self.sandbox_id = None
        self.started_at = None
        if not keep_error:
            self.last_error = None


class WorkspaceRuntimeManager:
    """每个 Workspace 至多一个 runtime server 的进程内注册表。"""

    def __init__(
        self,
        *,
        port: WorkspaceRuntimePort,
        sandbox: SandboxBackend,
        clock: Callable[[], float] = time.time,
        projection_source: RuntimeProjectionSource | None = None,
        projection_store: RuntimeProjectionStore | None = None,
        projection_renderer: RuntimeProjectionRenderer | None = None,
        credential_resolver: RuntimeCredentialResolver | None = None,
        publish_timeout: float = 10.0,
    ) -> None:
        self._port = port
        self._sandbox = sandbox
        self._clock = clock
        self._projection_source = projection_source
        self._projection_store = projection_store
        self._renderer = projection_renderer
        self._credentials = credential_resolver
        self._publish_timeout = publish_timeout
        self._entries: dict[str, _ManagedWorkspace] = {}
        self._registry_lock = threading.Lock()

    def register(self, spec: WorkspaceRuntimeSpec) -> WorkspaceRuntimeState:
        return self._ensure_entry(spec).snapshot()

    def get_state(self, workspace_id: str) -> WorkspaceRuntimeState:
        entry = self._entries.get(workspace_id)
        if entry is None:
            return WorkspaceRuntimeState(
                workspace_id=workspace_id, phase=RuntimePhase.STOPPED
            )
        return entry.snapshot()

    def list_states(self) -> tuple[WorkspaceRuntimeState, ...]:
        with self._registry_lock:
            return tuple(entry.snapshot() for entry in self._entries.values())

    async def ensure_started(self, spec: WorkspaceRuntimeSpec) -> WorkspaceRuntimeState:
        entry = self._ensure_entry(spec)
        async with entry.lock:
            if entry.handle is not None and entry.phase is RuntimePhase.HEALTHY:
                health = await self._probe(entry)
                if health.ready:
                    return entry.snapshot()
                entry.phase = RuntimePhase.UNHEALTHY
                entry.last_error = health.detail or "runtime health check failed"
            return await self._start_locked(entry)

    async def restart(self, spec: WorkspaceRuntimeSpec) -> WorkspaceRuntimeState:
        await self.stop(spec.workspace.workspace_id)
        return await self.ensure_started(spec)

    def publication_state(self, workspace_id: str) -> WorkspacePublicationState:
        entry = self._entries.get(workspace_id)
        if entry is None:
            return WorkspacePublicationState(
                workspace_id=workspace_id, phase=PublicationPhase.IDLE
            )
        return entry.publication_state()

    async def acquire_turn_permit(
        self,
        spec: WorkspaceRuntimeSpec,
        *,
        turn_id: str,
        runtime_session_id: str | None = None,
    ) -> TurnPermit:
        """在同一临界区确认投影已发布并登记活动 Turn；发布期间暂停放行。"""
        workspace_id = spec.workspace.workspace_id
        self._require_publication_configured(workspace_id)
        entry = self._ensure_entry(spec)
        deadline = time.monotonic() + self._publish_timeout
        await self._published_hash(entry)
        waited = False
        while True:
            async with entry.permit_lock:
                task = entry.publish_task
                if task is not None and not task.done():
                    publish = task
                else:
                    projection = await self._load_projection(entry)
                    if (
                        entry.publication is PublicationPhase.IDLE
                        and entry.published_hash == projection.projection_hash
                    ):
                        self._check_config_isolation(entry)
                        if entry.phase is not RuntimePhase.HEALTHY:
                            raise RuntimeUnavailableError(
                                workspace_id=workspace_id,
                                detail=entry.last_error
                                or "workspace runtime is not healthy",
                            )
                        permit_id = f"permit_{uuid4().hex}"
                        entry.active_turns[permit_id] = _ActiveTurn(
                            turn_id=turn_id, runtime_session_id=runtime_session_id
                        )
                        entry.drained.clear()
                        entry.last_activity = self._clock()
                        return TurnPermit(
                            permit_id=permit_id,
                            workspace_id=workspace_id,
                            turn_id=turn_id,
                            published_projection_hash=projection.projection_hash,
                        )
                    if waited:
                        raise RuntimePublishUnavailableError(
                            workspace_id=workspace_id,
                            detail="workspace configuration changed during publish",
                        )
                    publish = asyncio.create_task(
                        self._publish(entry, projection, deadline)
                    )
                    entry.publish_task = publish
                    publish.add_done_callback(
                        lambda done: self._on_publish_done(entry, done)
                    )
            remaining = max(0.001, deadline - time.monotonic())
            try:
                await asyncio.wait_for(asyncio.shield(publish), remaining)
            except TimeoutError as exc:
                raise RuntimePublishUnavailableError(
                    workspace_id=workspace_id,
                    detail="workspace configuration publish is still running",
                ) from exc
            except RuntimeUnavailableError as exc:
                raise RuntimePublishUnavailableError(
                    workspace_id=workspace_id, detail=exc.message
                ) from exc
            except Exception as exc:
                raise RuntimePublishUnavailableError(
                    workspace_id=workspace_id,
                    detail="workspace configuration publish failed",
                ) from exc
            waited = True

    async def bind_turn_runtime_session(
        self, *, workspace_id: str, turn_id: str, runtime_session_id: str
    ) -> None:
        entry = self._entries.get(workspace_id)
        if entry is None:
            return
        async with entry.permit_lock:
            for turn in entry.active_turns.values():
                if turn.turn_id == turn_id:
                    turn.runtime_session_id = runtime_session_id

    async def release_turn_permit(self, permit: TurnPermit) -> None:
        entry = self._entries.get(permit.workspace_id)
        if entry is None:
            return
        async with entry.permit_lock:
            entry.active_turns.pop(permit.permit_id, None)
            if not entry.active_turns:
                entry.drained.set()
            entry.last_activity = self._clock()

    def _require_publication_configured(self, workspace_id: str) -> None:
        if (
            self._projection_source is None
            or self._projection_store is None
            or self._renderer is None
            or self._credentials is None
        ):
            raise RuntimePublishUnavailableError(
                workspace_id=workspace_id,
                detail="workspace projection publication is not configured",
            )

    async def _publish(
        self,
        entry: _ManagedWorkspace,
        projection: RuntimeProjection,
        deadline: float,
    ) -> None:
        workspace_id = entry.spec.workspace.workspace_id
        renderer = self._renderer
        store = self._projection_store
        if renderer is None or store is None:
            raise RuntimePublishUnavailableError(workspace_id=workspace_id)
        try:
            async with entry.permit_lock:
                entry.publication = PublicationPhase.DRAINING
                entry.pending_hash = projection.projection_hash
                entry.publish_error = None
            await entry.drained.wait()
            self._check_config_isolation(entry)
            credentials = await self._resolve_credentials(projection)
            files = renderer.render(projection, credentials)
            entry.publication = PublicationPhase.RESTARTING
            await asyncio.to_thread(
                self._sandbox.write_runtime_files,
                runtime_home=self._config_home(entry),
                files=dict(files),
            )
            await self.restart(entry.spec)
            entry.publication = PublicationPhase.VERIFYING
            remaining = max(0.001, deadline - time.monotonic())
            readiness = await self._port.verify_location(
                self._verification_request(workspace_id, projection, timeout=remaining)
            )
            if not readiness.ready:
                raise RuntimeUnavailableError(
                    workspace_id=workspace_id,
                    detail=readiness.detail or "location is not ready after publish",
                )
            await store.save_published_hash(workspace_id, projection.projection_hash)
            entry.published_hash = projection.projection_hash
            entry.publication = PublicationPhase.IDLE
            entry.last_activity = self._clock()
        except Exception as exc:
            entry.publication = PublicationPhase.FAILED
            entry.publish_error = _error_text(exc)
            raise
        finally:
            entry.pending_hash = None

    def _on_publish_done(
        self, entry: _ManagedWorkspace, task: asyncio.Task[None]
    ) -> None:
        if entry.publish_task is task:
            entry.publish_task = None
        with contextlib.suppress(asyncio.CancelledError, Exception):
            task.exception()

    async def _load_projection(self, entry: _ManagedWorkspace) -> RuntimeProjection:
        workspace_id = entry.spec.workspace.workspace_id
        if self._projection_source is None:
            raise RuntimePublishUnavailableError(workspace_id=workspace_id)
        try:
            return await self._projection_source.load(workspace_id)
        except RuntimePublishUnavailableError:
            raise
        except Exception as exc:
            raise RuntimePublishUnavailableError(
                workspace_id=workspace_id, detail="failed to load workspace projection"
            ) from exc

    async def _published_hash(self, entry: _ManagedWorkspace) -> str | None:
        if entry.published_loaded:
            return entry.published_hash
        workspace_id = entry.spec.workspace.workspace_id
        if self._projection_store is None:
            raise RuntimePublishUnavailableError(workspace_id=workspace_id)
        try:
            entry.published_hash = await self._projection_store.get_published_hash(
                workspace_id
            )
        except Exception as exc:
            raise RuntimePublishUnavailableError(
                workspace_id=workspace_id,
                detail="failed to read published projection hash",
            ) from exc
        entry.published_loaded = True
        return entry.published_hash

    async def _resolve_credentials(
        self, projection: RuntimeProjection
    ) -> Mapping[str, str]:
        if self._credentials is None:
            return {}
        providers = tuple(
            sorted(
                {str(model["provider_id"]) for model in projection.document["models"]}
            )
        )
        return await self._credentials.resolve(providers)

    @staticmethod
    def _verification_request(
        workspace_id: str, projection: RuntimeProjection, *, timeout: float
    ) -> RuntimeLocationVerification:
        document = projection.document
        return RuntimeLocationVerification(
            workspace_id=workspace_id,
            agents=tuple(str(agent["internal_name"]) for agent in document["agents"]),
            models=tuple(
                RuntimeModelRef(
                    provider_id=str(model["provider_id"]),
                    model_id=str(model["provider_model_id"]),
                )
                for model in document["models"]
            ),
            skills=tuple(str(skill["name"]) for skill in document["skills"]),
            timeout=timeout,
        )

    @staticmethod
    def _config_home(entry: _ManagedWorkspace) -> str:
        return str(entry.spec.env["XDG_CONFIG_HOME"])

    def _check_config_isolation(self, entry: _ManagedWorkspace) -> None:
        workspace_id = entry.spec.workspace.workspace_id
        home = entry.spec.env.get("HOME")
        config_home = entry.spec.env.get("XDG_CONFIG_HOME")
        if not home or not config_home:
            raise RuntimeUnavailableError(
                workspace_id=workspace_id,
                detail="workspace runtime home is not isolated",
            )
        if config_home != str(Path(home) / "config"):
            raise RuntimeUnavailableError(
                workspace_id=workspace_id,
                detail="workspace runtime home is not isolated",
            )

    async def health(self, workspace_id: str) -> WorkspaceRuntimeState:
        entry = self._entries.get(workspace_id)
        if entry is None or entry.handle is None:
            return self.get_state(workspace_id)
        async with entry.lock:
            if entry.handle is None:
                return entry.snapshot()
            status = await asyncio.to_thread(self._sandbox.status, entry.handle)
            if status is not SandboxStatus.running:
                entry.phase = RuntimePhase.UNHEALTHY
                entry.last_error = f"sandbox is {status.value}"
                return entry.snapshot()
            health = await self._probe(entry)
            if not health.ready:
                entry.phase = RuntimePhase.UNHEALTHY
                entry.last_error = health.detail or "runtime health check failed"
                return entry.snapshot()
            entry.phase = RuntimePhase.HEALTHY
            entry.last_error = None
            return entry.snapshot()

    async def stop(self, workspace_id: str) -> WorkspaceRuntimeState:
        entry = self._entries.get(workspace_id)
        if entry is None:
            return self.get_state(workspace_id)
        async with entry.lock:
            if entry.handle is None:
                entry.reset(RuntimePhase.STOPPED)
                return entry.snapshot()
            entry.phase = RuntimePhase.STOPPING
            try:
                await self._cleanup_handle(entry)
            except RuntimeUnavailableError as exc:
                entry.reset(RuntimePhase.ERROR)
                entry.last_error = exc.message
                raise RuntimeUnavailableError(
                    workspace_id=workspace_id, detail="failed to stop workspace runtime"
                ) from exc
            entry.reset(RuntimePhase.STOPPED)
            return entry.snapshot()

    async def shutdown(self) -> None:
        for workspace_id, entry in tuple(self._entries.items()):
            task = entry.publish_task
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                entry.publication = PublicationPhase.IDLE
                entry.pending_hash = None
            try:
                await self.stop(workspace_id)
            except RuntimeUnavailableError:
                _logger.warning(
                    "workspace runtime %s did not stop cleanly", workspace_id
                )

    async def _start_locked(self, entry: _ManagedWorkspace) -> WorkspaceRuntimeState:
        entry.reset(RuntimePhase.STARTING)
        if entry.handle is not None:
            try:
                await self._cleanup_handle(entry)
            except RuntimeUnavailableError as exc:
                entry.reset(RuntimePhase.ERROR)
                entry.last_error = exc.message
                raise
        try:
            start = asyncio.ensure_future(
                asyncio.to_thread(
                    self._sandbox.start,
                    workspace_id=entry.spec.workspace.workspace_id,
                    workspace_path=entry.spec.host_directory,
                    env=dict(entry.spec.env),
                    replace_env=True,
                    command=list(entry.spec.command),
                    port=entry.spec.preferred_port,
                    stderr_log_path=entry.spec.stderr_log_path,
                )
            )
            handle = await asyncio.shield(start)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                entry.handle = await start
            await self._abort_start(entry, "workspace runtime start cancelled")
            raise
        except Exception as exc:
            entry.reset(RuntimePhase.ERROR)
            entry.last_error = str(exc)
            raise RuntimeUnavailableError(
                workspace_id=entry.spec.workspace.workspace_id,
                detail="failed to start workspace runtime",
            ) from exc

        entry.handle = handle
        try:
            endpoint = await asyncio.to_thread(self._sandbox.endpoint, handle)
            health = await self._connect(entry, endpoint)
        except BaseException as exc:
            await self._abort_start(entry, f"failed to bind workspace runtime: {exc}")
            if isinstance(exc, Exception):
                raise RuntimeUnavailableError(
                    workspace_id=entry.spec.workspace.workspace_id,
                    detail="failed to bind workspace runtime",
                ) from exc
            raise
        if not health.ready:
            message = health.detail or "workspace runtime is not ready"
            await self._abort_start(entry, message)
            raise RuntimeUnavailableError(
                workspace_id=entry.spec.workspace.workspace_id, detail=message
            )

        entry.phase = RuntimePhase.HEALTHY
        entry.base_url = endpoint.base_url
        entry.sandbox_id = handle.sandbox_id
        entry.started_at = self._clock()
        entry.last_error = None
        return entry.snapshot()

    async def _connect(
        self, entry: _ManagedWorkspace, endpoint: AdapterEndpoint
    ) -> RuntimeHealth:
        connection = RuntimeConnection(
            workspace=entry.spec.workspace, base_url=endpoint.base_url
        )
        workspace_id = entry.spec.workspace.workspace_id
        try:
            return await self._port.connect(connection)
        except RuntimeUnavailableError as exc:
            return RuntimeHealth(
                workspace_id=workspace_id, ready=False, detail=exc.message
            )
        except Exception as exc:
            _logger.exception(
                "runtime adapter failed to connect workspace %s", workspace_id
            )
            return RuntimeHealth(
                workspace_id=workspace_id, ready=False, detail=str(exc)
            )

    async def _probe(self, entry: _ManagedWorkspace) -> RuntimeHealth:
        workspace: RuntimeWorkspace = entry.spec.workspace
        try:
            return await self._port.health(workspace.workspace_id)
        except RuntimeUnavailableError as exc:
            return RuntimeHealth(
                workspace_id=workspace.workspace_id, ready=False, detail=exc.message
            )
        except Exception as exc:
            _logger.exception(
                "runtime health probe failed for workspace %s", workspace.workspace_id
            )
            return RuntimeHealth(
                workspace_id=workspace.workspace_id, ready=False, detail=str(exc)
            )

    async def _disconnect(self, workspace_id: str) -> None:
        try:
            await self._port.disconnect(workspace_id)
        except Exception:  # pragma: no cover - adapter 断连应尽量不阻塞停机
            _logger.exception("runtime adapter failed to disconnect %s", workspace_id)

    async def _cleanup_handle(self, entry: _ManagedWorkspace) -> None:
        handle = entry.handle
        if handle is None:
            return
        await self._disconnect(entry.spec.workspace.workspace_id)
        try:
            await asyncio.to_thread(self._sandbox.cleanup, handle)
        except Exception as exc:
            _logger.exception(
                "failed to clean workspace runtime %s",
                entry.spec.workspace.workspace_id,
            )
            raise RuntimeUnavailableError(
                workspace_id=entry.spec.workspace.workspace_id,
                detail="failed to clean workspace runtime",
            ) from exc
        entry.handle = None

    async def _abort_start(self, entry: _ManagedWorkspace, message: str) -> None:
        try:
            await self._cleanup_handle(entry)
        except RuntimeUnavailableError as exc:
            message = f"{message}; {exc.message}"
        entry.reset(RuntimePhase.ERROR)
        entry.last_error = message

    def _ensure_entry(self, spec: WorkspaceRuntimeSpec) -> _ManagedWorkspace:
        workspace_id = spec.workspace.workspace_id
        with self._registry_lock:
            entry = self._entries.get(workspace_id)
            if entry is None:
                entry = _ManagedWorkspace(spec=spec)
                self._entries[workspace_id] = entry
            elif entry.spec != spec:
                if entry.handle is not None:
                    raise RuntimeUnavailableError(
                        workspace_id=workspace_id,
                        detail="workspace runtime spec changed while running",
                    )
                entry.spec = spec
            return entry


def _error_text(exc: BaseException) -> str:
    message = getattr(exc, "message", None)
    return message if isinstance(message, str) and message else str(exc)
