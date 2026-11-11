from __future__ import annotations

import ast
import asyncio
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from witty_service.runtime.contracts import (
    RuntimeConnection,
    RuntimeHealth,
    RuntimeWorkspace,
)
from witty_service.runtime.errors import RuntimeUnavailableError
from witty_service.runtime.manager import (
    RuntimePhase,
    WorkspaceRuntimeManager,
    WorkspaceRuntimeSpec,
)
from witty_service.sandbox.base import (
    AdapterEndpoint,
    SandboxBackend,
    SandboxHandle,
    SandboxStatus,
)


class FakePort:
    def __init__(self) -> None:
        self.connections: list[RuntimeConnection] = []
        self.disconnects: list[str] = []
        self.ready = True
        self.connect_error: Exception | None = None
        self.health_gate: threading.Event | None = None

    async def connect(self, connection: RuntimeConnection) -> RuntimeHealth:
        self.connections.append(connection)
        if self.connect_error is not None:
            raise self.connect_error
        return RuntimeHealth(
            workspace_id=connection.workspace.workspace_id, ready=self.ready
        )

    async def disconnect(self, workspace_id: str) -> None:
        self.disconnects.append(workspace_id)

    async def health(self, workspace_id: str) -> RuntimeHealth:
        if self.health_gate is not None:
            await asyncio.to_thread(self.health_gate.wait, 2)
        return RuntimeHealth(workspace_id=workspace_id, ready=self.ready)


class FakeSandbox(SandboxBackend):
    sandbox_type = "fake"

    def __init__(self) -> None:
        self.started: list[dict[str, object]] = []
        self.cleaned: list[str] = []
        self.status_value = SandboxStatus.running
        self.start_error: Exception | None = None
        self.endpoint_error: Exception | None = None
        self.cleanup_error: Exception | None = None
        self.start_delay = 0.0
        self.start_lock = threading.Lock()
        self.start_calls = 0

    def start(
        self,
        *,
        workspace_id: str,
        workspace_path: str,
        env: dict[str, str] | None = None,
        **kwargs: object,
    ) -> SandboxHandle:
        with self.start_lock:
            self.start_calls += 1
            if self.start_delay:
                time.sleep(self.start_delay)
            self.started.append(
                {
                    "workspace_id": workspace_id,
                    "workspace_path": workspace_path,
                    "env": env,
                    **kwargs,
                }
            )
        if self.start_error is not None:
            raise self.start_error
        return SandboxHandle(
            sandbox_id=f"sbx-{workspace_id}",
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            metadata={"base_url": f"http://127.0.0.1:4100/{workspace_id}"},
        )

    def stop(self, handle: SandboxHandle | str, **kwargs: object) -> None:
        self.cleaned.append(_handle_id(handle))

    def status(self, handle: SandboxHandle | str, **kwargs: object) -> SandboxStatus:
        return self.status_value

    def endpoint(
        self, handle: SandboxHandle | str, **kwargs: object
    ) -> AdapterEndpoint:
        if self.endpoint_error is not None:
            raise self.endpoint_error
        if not isinstance(handle, SandboxHandle):
            raise TypeError("handle must be a SandboxHandle")
        return AdapterEndpoint(base_url=str(handle.metadata["base_url"]))

    def cleanup(self, handle: SandboxHandle | str, **kwargs: object) -> None:
        if self.cleanup_error is not None:
            raise self.cleanup_error
        self.cleaned.append(_handle_id(handle))


def _handle_id(handle: SandboxHandle | str) -> str:
    return handle if isinstance(handle, str) else handle.sandbox_id


def _spec(
    workspace_id: str = "ws_a", *, host_directory: str = "/srv/ws_a"
) -> WorkspaceRuntimeSpec:
    return WorkspaceRuntimeSpec(
        workspace=RuntimeWorkspace(
            workspace_id=workspace_id, runtime_directory=host_directory
        ),
        host_directory=host_directory,
        command=("opencode", "serve", "--port", "{port}"),
        env={"HOME": f"{host_directory}/runtime"},
        preferred_port=4100,
    )


def test_manager_does_not_start_until_requested() -> None:
    sandbox = FakeSandbox()
    manager = WorkspaceRuntimeManager(port=FakePort(), sandbox=sandbox)

    manager.register(_spec())

    assert manager.get_state("ws_a").phase is RuntimePhase.STOPPED
    assert sandbox.started == []


@pytest.mark.asyncio
async def test_ensure_started_is_lazy_and_idempotent() -> None:
    port = FakePort()
    sandbox = FakeSandbox()
    manager = WorkspaceRuntimeManager(port=port, sandbox=sandbox)
    spec = _spec()

    state = await manager.ensure_started(spec)
    again = await manager.ensure_started(spec)

    assert state.phase is RuntimePhase.HEALTHY
    assert again.phase is RuntimePhase.HEALTHY
    assert len(sandbox.started) == 1
    assert len(port.connections) == 1
    assert sandbox.started[0]["command"] == ["opencode", "serve", "--port", "{port}"]
    assert sandbox.started[0]["port"] == 4100


@pytest.mark.asyncio
async def test_concurrent_ensure_starts_one_server() -> None:
    sandbox = FakeSandbox()
    sandbox.start_delay = 0.05
    manager = WorkspaceRuntimeManager(port=FakePort(), sandbox=sandbox)
    spec = _spec()

    await asyncio.gather(
        manager.ensure_started(spec),
        manager.ensure_started(spec),
        manager.ensure_started(spec),
    )

    assert sandbox.start_calls == 1


@pytest.mark.asyncio
async def test_health_reflects_sandbox_and_adapter() -> None:
    port = FakePort()
    sandbox = FakeSandbox()
    manager = WorkspaceRuntimeManager(port=port, sandbox=sandbox)
    await manager.ensure_started(_spec())

    sandbox.status_value = SandboxStatus.stopped
    state = await manager.health("ws_a")

    assert state.phase is RuntimePhase.UNHEALTHY
    assert state.last_error == "sandbox is stopped"


@pytest.mark.asyncio
async def test_restart_stops_then_starts_again() -> None:
    port = FakePort()
    sandbox = FakeSandbox()
    manager = WorkspaceRuntimeManager(port=port, sandbox=sandbox)
    spec = _spec()
    await manager.ensure_started(spec)

    state = await manager.restart(spec)

    assert state.phase is RuntimePhase.HEALTHY
    assert sandbox.cleaned == ["sbx-ws_a"]
    assert len(sandbox.started) == 2
    assert port.disconnects == ["ws_a"]


@pytest.mark.asyncio
async def test_start_failure_keeps_workspace_error_and_no_connection() -> None:
    sandbox = FakeSandbox()
    sandbox.start_error = RuntimeError("spawn failed")
    port = FakePort()
    manager = WorkspaceRuntimeManager(port=port, sandbox=sandbox)

    with pytest.raises(RuntimeUnavailableError):
        await manager.ensure_started(_spec())

    assert manager.get_state("ws_a").phase is RuntimePhase.ERROR
    assert manager.get_state("ws_a").last_error == "spawn failed"
    assert port.connections == []


@pytest.mark.asyncio
async def test_adapter_failure_cleans_up_process() -> None:
    port = FakePort()
    port.connect_error = RuntimeUnavailableError(
        workspace_id="ws_a", detail="not ready"
    )
    sandbox = FakeSandbox()
    manager = WorkspaceRuntimeManager(port=port, sandbox=sandbox)

    with pytest.raises(RuntimeUnavailableError):
        await manager.ensure_started(_spec())

    assert sandbox.cleaned == ["sbx-ws_a"]
    assert manager.get_state("ws_a").phase is RuntimePhase.ERROR


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_scoped_to_workspace() -> None:
    port = FakePort()
    sandbox = FakeSandbox()
    manager = WorkspaceRuntimeManager(port=port, sandbox=sandbox)
    await manager.ensure_started(_spec("ws_a"))
    await manager.ensure_started(_spec("ws_b"))

    stopped = await manager.stop("ws_a")
    stopped_again = await manager.stop("ws_a")

    assert stopped.phase is RuntimePhase.STOPPED
    assert stopped_again.phase is RuntimePhase.STOPPED
    assert sandbox.cleaned == ["sbx-ws_a"]
    assert manager.get_state("ws_b").phase is RuntimePhase.HEALTHY


@pytest.mark.asyncio
async def test_endpoint_failure_cleans_up_and_reports_error() -> None:
    sandbox = FakeSandbox()
    sandbox.endpoint_error = RuntimeError("no endpoint")
    manager = WorkspaceRuntimeManager(port=FakePort(), sandbox=sandbox)

    with pytest.raises(RuntimeUnavailableError):
        await manager.ensure_started(_spec())

    assert sandbox.cleaned == ["sbx-ws_a"]
    assert manager.get_state("ws_a").phase is RuntimePhase.ERROR


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_handle_for_retry() -> None:
    sandbox = FakeSandbox()
    manager = WorkspaceRuntimeManager(port=FakePort(), sandbox=sandbox)
    await manager.ensure_started(_spec())
    sandbox.cleanup_error = RuntimeError("still alive")

    with pytest.raises(RuntimeUnavailableError):
        await manager.stop("ws_a")

    assert manager.get_state("ws_a").phase is RuntimePhase.ERROR
    entry = manager._entries["ws_a"]
    assert entry.handle is not None


@pytest.mark.asyncio
async def test_spec_change_while_running_requires_restart() -> None:
    manager = WorkspaceRuntimeManager(port=FakePort(), sandbox=FakeSandbox())
    await manager.ensure_started(_spec("ws_a"))

    with pytest.raises(RuntimeUnavailableError, match="spec changed"):
        await manager.ensure_started(_spec("ws_a", host_directory="/srv/other"))


@pytest.mark.asyncio
async def test_unknown_workspace_is_reported_as_stopped() -> None:
    manager = WorkspaceRuntimeManager(port=FakePort(), sandbox=FakeSandbox())

    assert manager.get_state("missing").phase is RuntimePhase.STOPPED
    assert (await manager.health("missing")).phase is RuntimePhase.STOPPED
    assert (await manager.stop("missing")).phase is RuntimePhase.STOPPED


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        self.value += 1.0
        return self.value


@pytest.mark.asyncio
async def test_state_records_generation_timestamp() -> None:
    clock = _Clock()
    manager = WorkspaceRuntimeManager(
        port=FakePort(), sandbox=FakeSandbox(), clock=clock
    )

    state = await manager.ensure_started(_spec())

    assert state.started_at == 101.0
    assert state.sandbox_id == "sbx-ws_a"
    assert state.base_url == "http://127.0.0.1:4100/ws_a"


def test_spec_requires_launch_command() -> None:
    with pytest.raises(ValueError):
        replace(_spec(), command=())
    with pytest.raises(ValueError):
        replace(_spec(), host_directory=" ")


def test_bootstrap_is_the_only_runtime_import_boundary() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src" / "witty_service"
    offenders = []
    for path in source_root.rglob("*.py"):
        if path.name == "bootstrap.py":
            continue
        tree = ast.parse(path.read_text())
        modules = [
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        ]
        modules += [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        if any(module.split(".")[0] == "witty_agent_server" for module in modules):
            offenders.append(path.relative_to(source_root).as_posix())

    assert offenders == []


def test_bootstrap_builds_manager_without_starting_processes() -> None:
    from witty_service.bootstrap import build_workspace_runtime_manager

    manager = build_workspace_runtime_manager(sandbox=FakeSandbox())

    assert isinstance(manager, WorkspaceRuntimeManager)
    assert manager.list_states() == ()
