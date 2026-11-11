from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping

import pytest

from witty_service.runtime.contracts import (
    ReplayQuery,
    RuntimeConnection,
    RuntimeEventEnvelope,
    RuntimeEventReplay,
    RuntimeHealth,
    RuntimeLocationReadiness,
    RuntimeLocationVerification,
    RuntimeWorkspace,
)
from witty_service.runtime.events import RuntimeResyncReason, RuntimeResyncRequired
from witty_service.runtime.manager import (
    RuntimePhase,
    WorkspaceRuntimeManager,
    WorkspaceRuntimeSpec,
)
from witty_service.runtime.projection import (
    ProjectionAgent,
    ProjectionInputs,
    ProjectionModel,
    RuntimeProjection,
    build_projection,
)
from witty_service.runtime.publication import PublicationPhase
from witty_service.sandbox.base import (
    AdapterEndpoint,
    SandboxBackend,
    SandboxHandle,
    SandboxStatus,
)


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakePort:
    def __init__(self) -> None:
        self.streams: dict[str, list[asyncio.Queue[RuntimeEventEnvelope]]] = {}
        self.verify_gate: asyncio.Event | None = None

    async def connect(self, connection: RuntimeConnection) -> RuntimeHealth:
        return RuntimeHealth(workspace_id=connection.workspace.workspace_id, ready=True)

    async def disconnect(self, workspace_id: str) -> None:
        return None

    async def health(self, workspace_id: str) -> RuntimeHealth:
        return RuntimeHealth(workspace_id=workspace_id, ready=True)

    def subscribe(self, workspace_id: str) -> AsyncIterator[RuntimeEventEnvelope]:
        queue: asyncio.Queue[RuntimeEventEnvelope] = asyncio.Queue()
        self.streams.setdefault(workspace_id, []).append(queue)
        return self._stream(workspace_id, queue)

    async def _stream(
        self, workspace_id: str, queue: asyncio.Queue[RuntimeEventEnvelope]
    ) -> AsyncIterator[RuntimeEventEnvelope]:
        try:
            while True:
                yield await queue.get()
        finally:
            self.streams[workspace_id].remove(queue)

    async def replay_events(self, query: ReplayQuery) -> RuntimeEventReplay:
        return RuntimeEventReplay(events=(), has_more=False)

    async def verify_location(
        self, request: RuntimeLocationVerification
    ) -> RuntimeLocationReadiness:
        if self.verify_gate is not None:
            await self.verify_gate.wait()
        return RuntimeLocationReadiness(
            workspace_id=request.workspace_id,
            ready=True,
            agents=request.agents,
            models=request.models,
            skills=request.skills,
        )


class FakeSandbox(SandboxBackend):
    sandbox_type = "fake"

    def __init__(self) -> None:
        self.running: set[str] = set()
        self.ports: dict[str, int] = {}
        self.starts: list[str] = []
        self.cleanups: list[str] = []
        self.cleanup_failures = 0

    def start(
        self,
        *,
        workspace_id: str,
        workspace_path: str,
        env: dict[str, str] | None = None,
        replace_env: bool = False,
        **kwargs: object,
    ) -> SandboxHandle:
        port = kwargs.get("port")
        if not isinstance(port, int):
            raise AssertionError("runtime spec must pin a preferred port")
        if port in self.ports.values():
            raise AssertionError(f"port {port} is already taken")
        self.starts.append(workspace_id)
        self.running.add(workspace_id)
        self.ports[workspace_id] = port
        return SandboxHandle(
            sandbox_id=f"sbx-{workspace_id}",
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            metadata={"base_url": f"http://127.0.0.1:{port}"},
        )

    def stop(self, handle: SandboxHandle | str, **kwargs: object) -> None:
        self.cleanup(handle)

    def status(self, handle: SandboxHandle | str, **kwargs: object) -> SandboxStatus:
        if _workspace_id(handle) in self.running:
            return SandboxStatus.running
        return SandboxStatus.stopped

    def endpoint(
        self, handle: SandboxHandle | str, **kwargs: object
    ) -> AdapterEndpoint:
        assert isinstance(handle, SandboxHandle)
        return AdapterEndpoint(base_url=str(handle.metadata["base_url"]))

    def cleanup(self, handle: SandboxHandle | str, **kwargs: object) -> None:
        if self.cleanup_failures:
            self.cleanup_failures -= 1
            raise RuntimeError("cleanup failed")
        workspace_id = _workspace_id(handle)
        self.cleanups.append(workspace_id)
        self.running.discard(workspace_id)
        self.ports.pop(workspace_id, None)

    def write_runtime_files(
        self, *, runtime_home: str, files: Mapping[str, str]
    ) -> None:
        return None

    def crash(self, workspace_id: str) -> None:
        self.running.discard(workspace_id)


def _workspace_id(handle: SandboxHandle | str) -> str:
    return handle.workspace_id if isinstance(handle, SandboxHandle) else handle


def _projection() -> RuntimeProjection:
    return build_projection(
        ProjectionInputs(
            agents=(
                ProjectionAgent(
                    definition_id="agent-1",
                    name="Primary",
                    description="",
                    system_prompt="You are Witty.",
                    kind="primary",
                    source_type="builtin",
                ),
            ),
            models=(
                ProjectionModel(
                    model_id="model-1",
                    display_name="Model One",
                    provider_id="provider-1",
                    provider_model_id="model-one",
                    max_tokens=4096,
                    temperature=0.1,
                    is_default=True,
                    secret_revision=1,
                ),
            ),
            skills=(),
        )
    )


class FakeProjectionSource:
    def __init__(self) -> None:
        self.projection = _projection()

    async def load(self, workspace_id: str) -> RuntimeProjection:
        return self.projection


class FakeProjectionStore:
    def __init__(self, published: str | None) -> None:
        self.published = published

    async def get_published_hash(self, workspace_id: str) -> str | None:
        return self.published

    async def save_published_hash(
        self, workspace_id: str, projection_hash: str
    ) -> None:
        self.published = projection_hash


class FakeRenderer:
    def render(self, projection, credentials) -> dict[str, str]:
        return {"opencode/opencode.json": projection.canonical_json}


class FakeCredentials:
    async def resolve(self, provider_ids: tuple[str, ...]) -> dict[str, str]:
        return {provider_id: "secret" for provider_id in provider_ids}


class Harness:
    def __init__(
        self,
        *,
        idle_timeout: float = 60.0,
        lifecycle_interval: float = 3600.0,
        published: bool = True,
    ) -> None:
        self.clock = FakeClock()
        self.port = FakePort()
        self.sandbox = FakeSandbox()
        source = FakeProjectionSource()
        self.manager = WorkspaceRuntimeManager(
            port=self.port,
            sandbox=self.sandbox,
            clock=self.clock,
            projection_source=source,
            projection_store=FakeProjectionStore(
                source.projection.projection_hash if published else None
            ),
            projection_renderer=FakeRenderer(),
            credential_resolver=FakeCredentials(),
            publish_timeout=5.0,
            event_reconnect_delay=0.01,
            idle_timeout=idle_timeout,
            lifecycle_interval=lifecycle_interval,
        )

    async def close(self) -> None:
        await self.manager.shutdown()


def _spec(workspace_id: str, port: int) -> WorkspaceRuntimeSpec:
    home = f"/srv/{workspace_id}/runtime"
    return WorkspaceRuntimeSpec(
        workspace=RuntimeWorkspace(
            workspace_id=workspace_id, runtime_directory=f"/srv/{workspace_id}"
        ),
        host_directory=f"/srv/{workspace_id}",
        command=("opencode", "serve", "--port", "{port}"),
        env={"HOME": home, "XDG_CONFIG_HOME": f"{home}/config"},
        preferred_port=port,
    )


async def _next(stream: AsyncIterator[object]) -> object:
    return await asyncio.wait_for(stream.__anext__(), timeout=1)


async def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_idle_stop_releases_port_and_is_repeatable() -> None:
    harness = Harness(idle_timeout=60.0)
    spec = _spec("ws_a", 4100)
    await harness.manager.ensure_started(spec)
    assert harness.sandbox.ports == {"ws_a": 4100}

    stream = harness.manager.subscribe_events("ws_a")
    pending = asyncio.create_task(_next(stream))
    await asyncio.sleep(0.01)

    harness.clock.advance(61)
    await harness.manager.reconcile()

    state = harness.manager.get_state("ws_a")
    assert state.phase is RuntimePhase.STOPPED
    assert harness.sandbox.ports == {}
    assert harness.sandbox.cleanups == ["ws_a"]
    item = await pending
    assert isinstance(item, RuntimeResyncRequired)
    assert item.reason is RuntimeResyncReason.RUNTIME_STOPPED
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()

    await harness.manager.reconcile()
    assert harness.sandbox.cleanups == ["ws_a"]

    await harness.manager.ensure_started(spec)
    assert harness.manager.get_state("ws_a").phase is RuntimePhase.HEALTHY
    assert harness.sandbox.ports == {"ws_a": 4100}
    assert harness.sandbox.starts == ["ws_a", "ws_a"]
    await harness.close()


@pytest.mark.asyncio
async def test_crashed_runtime_is_marked_unavailable_without_restart() -> None:
    harness = Harness()
    await harness.manager.ensure_started(_spec("ws_a", 4100))
    await harness.manager.ensure_started(_spec("ws_b", 4101))

    stream = harness.manager.subscribe_events("ws_a")
    pending = asyncio.create_task(_next(stream))
    await asyncio.sleep(0.01)

    harness.sandbox.crash("ws_a")
    await harness.manager.reconcile()

    state = harness.manager.get_state("ws_a")
    assert state.phase is RuntimePhase.UNHEALTHY
    assert state.last_error == "sandbox is stopped"
    assert harness.sandbox.cleanups == ["ws_a"]
    item = await pending
    assert isinstance(item, RuntimeResyncRequired)
    assert item.reason is RuntimeResyncReason.RUNTIME_EXITED
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()

    assert harness.manager.get_state("ws_b").phase is RuntimePhase.HEALTHY
    await harness.manager.reconcile()
    assert harness.sandbox.starts == ["ws_a", "ws_b"]
    await harness.close()


@pytest.mark.asyncio
async def test_active_turn_blocks_idle_stop() -> None:
    harness = Harness(idle_timeout=60.0)
    spec = _spec("ws_a", 4100)
    await harness.manager.ensure_started(spec)
    permit = await harness.manager.acquire_turn_permit(
        spec, turn_id="turn-1", runtime_session_id="ses_1"
    )

    harness.clock.advance(120)
    await harness.manager.reconcile()
    assert harness.manager.get_state("ws_a").phase is RuntimePhase.HEALTHY

    await harness.manager.release_turn_permit(permit)
    harness.clock.advance(59)
    await harness.manager.reconcile()
    assert harness.manager.get_state("ws_a").phase is RuntimePhase.HEALTHY

    harness.clock.advance(2)
    await harness.manager.reconcile()
    assert harness.manager.get_state("ws_a").phase is RuntimePhase.STOPPED
    await harness.close()


@pytest.mark.asyncio
async def test_publishing_blocks_idle_stop() -> None:
    harness = Harness(idle_timeout=60.0, published=False)
    spec = _spec("ws_a", 4100)
    harness.port.verify_gate = asyncio.Event()
    permit_task = asyncio.create_task(
        harness.manager.acquire_turn_permit(spec, turn_id="turn-1")
    )
    await _wait_until(
        lambda: (
            harness.manager.publication_state("ws_a").phase
            is PublicationPhase.VERIFYING
        )
    )

    harness.clock.advance(120)
    await harness.manager.reconcile()
    assert harness.manager.get_state("ws_a").phase is RuntimePhase.HEALTHY
    assert harness.sandbox.ports == {"ws_a": 4100}

    assert harness.port.verify_gate is not None
    harness.port.verify_gate.set()
    permit = await asyncio.wait_for(permit_task, timeout=1)
    await harness.manager.release_turn_permit(permit)
    assert harness.manager.publication_state("ws_a").phase is PublicationPhase.IDLE
    await harness.close()


@pytest.mark.asyncio
async def test_lifecycle_monitor_reconciles_without_manual_call() -> None:
    harness = Harness(idle_timeout=60.0, lifecycle_interval=0.01)
    await harness.manager.ensure_started(_spec("ws_a", 4100))
    harness.clock.advance(61)

    await _wait_until(lambda: harness.sandbox.ports == {})
    assert harness.manager.get_state("ws_a").phase is RuntimePhase.STOPPED
    await harness.close()


@pytest.mark.asyncio
async def test_failed_idle_stop_is_retried_on_next_reconcile() -> None:
    harness = Harness(idle_timeout=60.0)
    await harness.manager.ensure_started(_spec("ws_a", 4100))
    harness.sandbox.cleanup_failures = 1
    harness.clock.advance(61)

    await harness.manager.reconcile()
    assert harness.manager.get_state("ws_a").phase is RuntimePhase.ERROR
    assert harness.sandbox.ports == {"ws_a": 4100}

    await harness.manager.reconcile()
    assert harness.manager.get_state("ws_a").phase is RuntimePhase.STOPPED
    assert harness.sandbox.ports == {}
    await harness.close()
