from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from witty_service.runtime.contracts import (
    RuntimeConnection,
    RuntimeHealth,
    RuntimeWorkspace,
    WorkspaceRuntimePort,
)
from witty_service.runtime.errors import RuntimeUnavailableError
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
class _ManagedWorkspace:
    spec: WorkspaceRuntimeSpec
    phase: RuntimePhase = RuntimePhase.STOPPED
    base_url: str | None = None
    sandbox_id: str | None = None
    started_at: float | None = None
    last_error: str | None = None
    handle: SandboxHandle | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def snapshot(self) -> WorkspaceRuntimeState:
        return WorkspaceRuntimeState(
            workspace_id=self.spec.workspace.workspace_id,
            phase=self.phase,
            base_url=self.base_url,
            sandbox_id=self.sandbox_id,
            started_at=self.started_at,
            last_error=self.last_error,
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
    ) -> None:
        self._port = port
        self._sandbox = sandbox
        self._clock = clock
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
        for workspace_id in tuple(self._entries):
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
            handle = await asyncio.to_thread(
                self._sandbox.start,
                workspace_id=entry.spec.workspace.workspace_id,
                workspace_path=entry.spec.host_directory,
                env=dict(entry.spec.env),
                command=list(entry.spec.command),
                port=entry.spec.preferred_port,
            )
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
        except Exception as exc:
            await self._abort_start(entry, f"failed to bind workspace runtime: {exc}")
            raise RuntimeUnavailableError(
                workspace_id=entry.spec.workspace.workspace_id,
                detail="failed to bind workspace runtime",
            ) from exc
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
