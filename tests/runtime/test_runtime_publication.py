from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

import pytest

from witty_service.runtime.contracts import (
    RuntimeConnection,
    RuntimeHealth,
    RuntimeLocationReadiness,
    RuntimeLocationVerification,
    RuntimeWorkspace,
)
from witty_service.runtime.errors import RuntimePublishUnavailableError
from witty_service.runtime.manager import WorkspaceRuntimeManager, WorkspaceRuntimeSpec
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


def _projection(prompt: str = "You are Witty.") -> RuntimeProjection:
    return build_projection(
        ProjectionInputs(
            agents=(
                ProjectionAgent(
                    definition_id="agent-1",
                    name="Primary",
                    description="",
                    system_prompt=prompt,
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
    def __init__(self, projection: RuntimeProjection) -> None:
        self.projection = projection

    async def load(self, workspace_id: str) -> RuntimeProjection:
        return self.projection


class FakeProjectionStore:
    def __init__(self) -> None:
        self.published: str | None = None
        self.saved: list[tuple[str, str]] = []

    async def get_published_hash(self, workspace_id: str) -> str | None:
        return self.published

    async def save_published_hash(
        self, workspace_id: str, projection_hash: str
    ) -> None:
        self.published = projection_hash
        self.saved.append((workspace_id, projection_hash))


class FakeRenderer:
    def __init__(self) -> None:
        self.renders: list[str] = []

    def render(self, projection, credentials) -> dict[str, str]:
        assert credentials["provider-1"] == "secret"
        self.renders.append(projection.projection_hash)
        return {"opencode/opencode.json": projection.canonical_json}


class FakeCredentials:
    async def resolve(self, provider_ids: tuple[str, ...]) -> dict[str, str]:
        return {provider_id: "secret" for provider_id in provider_ids}


class FakePort:
    def __init__(self) -> None:
        self.connections: list[RuntimeConnection] = []
        self.disconnects: list[str] = []
        self.verify_calls: list[RuntimeLocationVerification] = []
        self.verify_ready = True
        self.verify_detail: str | None = None

    async def connect(self, connection: RuntimeConnection) -> RuntimeHealth:
        self.connections.append(connection)
        return RuntimeHealth(workspace_id=connection.workspace.workspace_id, ready=True)

    async def disconnect(self, workspace_id: str) -> None:
        self.disconnects.append(workspace_id)

    async def health(self, workspace_id: str) -> RuntimeHealth:
        return RuntimeHealth(workspace_id=workspace_id, ready=True)

    async def verify_location(
        self, request: RuntimeLocationVerification
    ) -> RuntimeLocationReadiness:
        self.verify_calls.append(request)
        return RuntimeLocationReadiness(
            workspace_id=request.workspace_id,
            ready=self.verify_ready,
            agents=request.agents,
            models=request.models,
            skills=request.skills,
            detail=None if self.verify_ready else self.verify_detail or "not ready",
        )


class FakeSandbox(SandboxBackend):
    sandbox_type = "fake"

    def __init__(self) -> None:
        self.started: list[dict[str, object]] = []
        self.cleaned: list[str] = []
        self.written: list[tuple[str, dict[str, str]]] = []
        self.status_value = SandboxStatus.running
        self.write_hook: Callable[[], None] | None = None

    def start(
        self,
        *,
        workspace_id: str,
        workspace_path: str,
        env: dict[str, str] | None = None,
        **kwargs: object,
    ) -> SandboxHandle:
        self.started.append(
            {"workspace_id": workspace_id, "workspace_path": workspace_path, "env": env}
        )
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
        if not isinstance(handle, SandboxHandle):
            raise TypeError("handle must be a SandboxHandle")
        return AdapterEndpoint(base_url=str(handle.metadata["base_url"]))

    def cleanup(self, handle: SandboxHandle | str, **kwargs: object) -> None:
        self.cleaned.append(_handle_id(handle))

    def write_runtime_files(
        self, *, runtime_home: str, files: Mapping[str, str]
    ) -> None:
        if self.write_hook is not None:
            self.write_hook()
        self.written.append((runtime_home, dict(files)))


def _handle_id(handle: SandboxHandle | str) -> str:
    return handle if isinstance(handle, str) else handle.sandbox_id


def _spec(workspace_id: str = "ws_a", *, isolated: bool = True) -> WorkspaceRuntimeSpec:
    home = f"/srv/{workspace_id}/runtime"
    env = {"HOME": home}
    if isolated:
        env["XDG_CONFIG_HOME"] = f"{home}/config"
    return WorkspaceRuntimeSpec(
        workspace=RuntimeWorkspace(
            workspace_id=workspace_id, runtime_directory=f"/srv/{workspace_id}"
        ),
        host_directory=f"/srv/{workspace_id}",
        command=("opencode", "serve", "--port", "{port}"),
        env=env,
        preferred_port=4100,
    )


class Harness:
    def __init__(
        self,
        *,
        projection: RuntimeProjection | None = None,
        publish_timeout: float = 5.0,
    ) -> None:
        self.projection = projection or _projection()
        self.source = FakeProjectionSource(self.projection)
        self.port = FakePort()
        self.sandbox = FakeSandbox()
        self.store = FakeProjectionStore()
        self.renderer = FakeRenderer()
        self.manager = WorkspaceRuntimeManager(
            port=self.port,
            sandbox=self.sandbox,
            projection_source=self.source,
            projection_store=self.store,
            projection_renderer=self.renderer,
            credential_resolver=FakeCredentials(),
            publish_timeout=publish_timeout,
        )

    def change_projection(self, prompt: str) -> RuntimeProjection:
        self.source.projection = _projection(prompt)
        return self.source.projection


@pytest.mark.asyncio
async def test_first_permit_materializes_restarts_and_verifies() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)

    permit = await harness.manager.acquire_turn_permit(spec, turn_id="turn-1")

    assert permit.published_projection_hash == harness.projection.projection_hash
    assert harness.store.saved == [("ws_a", harness.projection.projection_hash)]
    assert harness.sandbox.written == [
        (
            "/srv/ws_a/runtime/config",
            {"opencode/opencode.json": harness.projection.canonical_json},
        )
    ]
    assert len(harness.sandbox.started) == 2
    assert harness.port.verify_calls[0].agents == ("ag_agent-1",)
    assert harness.port.verify_calls[0].skills == ()
    assert harness.port.verify_calls[0].timeout <= 5.0
    state = harness.manager.publication_state("ws_a")
    assert state.phase is PublicationPhase.IDLE
    assert state.active_turns == 1


@pytest.mark.asyncio
async def test_published_hash_is_reused_without_restarting() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)
    permit = await harness.manager.acquire_turn_permit(spec, turn_id="turn-1")
    await harness.manager.release_turn_permit(permit)

    second = await harness.manager.acquire_turn_permit(spec, turn_id="turn-2")

    assert second.published_projection_hash == permit.published_projection_hash
    assert len(harness.sandbox.started) == 2
    assert len(harness.store.saved) == 1


@pytest.mark.asyncio
async def test_duplicate_turn_id_keeps_each_permit_registered() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)

    first = await harness.manager.acquire_turn_permit(spec, turn_id="turn-1")
    second = await harness.manager.acquire_turn_permit(spec, turn_id="turn-1")
    assert harness.manager.publication_state("ws_a").active_turns == 2

    await harness.manager.release_turn_permit(first)
    assert harness.manager.publication_state("ws_a").active_turns == 1

    await harness.manager.release_turn_permit(second)
    assert harness.manager.publication_state("ws_a").active_turns == 0


@pytest.mark.asyncio
async def test_publish_waits_for_active_turn_before_restarting() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)
    first = await harness.manager.acquire_turn_permit(spec, turn_id="turn-1")
    changed = harness.change_projection("Changed prompt.")

    waiter = asyncio.create_task(
        harness.manager.acquire_turn_permit(spec, turn_id="turn-2")
    )
    await asyncio.sleep(0.05)
    assert not waiter.done()
    assert len(harness.sandbox.started) == 2

    await harness.manager.release_turn_permit(first)
    second = await asyncio.wait_for(waiter, timeout=2)

    assert second.published_projection_hash == changed.projection_hash
    assert len(harness.sandbox.started) == 3
    assert len(harness.store.saved) == 2


@pytest.mark.asyncio
async def test_config_change_during_publish_fails_closed_until_next_turn() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)
    changed = _projection("Changed prompt.")
    harness.sandbox.write_hook = lambda: setattr(harness.source, "projection", changed)

    with pytest.raises(RuntimePublishUnavailableError, match="changed during publish"):
        await harness.manager.acquire_turn_permit(spec, turn_id="turn-1")

    assert harness.store.published == harness.projection.projection_hash
    assert harness.manager.publication_state("ws_a").active_turns == 0

    permit = await harness.manager.acquire_turn_permit(spec, turn_id="turn-2")

    assert permit.published_projection_hash == changed.projection_hash


@pytest.mark.asyncio
async def test_publish_timeout_does_not_register_turn_and_keeps_running() -> None:
    harness = Harness(publish_timeout=0.05)
    spec = _spec()
    await harness.manager.ensure_started(spec)
    first = await harness.manager.acquire_turn_permit(spec, turn_id="turn-1")
    changed = harness.change_projection("Changed prompt.")

    with pytest.raises(RuntimePublishUnavailableError, match="still running"):
        await harness.manager.acquire_turn_permit(spec, turn_id="turn-2")

    state = harness.manager.publication_state("ws_a")
    assert state.active_turns == 1
    assert state.pending_projection_hash == changed.projection_hash

    await harness.manager.release_turn_permit(first)
    for _ in range(100):
        if harness.store.published == changed.projection_hash:
            break
        await asyncio.sleep(0.01)
    assert harness.store.published == changed.projection_hash


@pytest.mark.asyncio
async def test_failed_verification_keeps_old_hash_and_retries_next_turn() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)
    harness.port.verify_ready = False
    harness.port.verify_detail = "step.started never appeared"

    with pytest.raises(RuntimePublishUnavailableError, match=r"step\.started"):
        await harness.manager.acquire_turn_permit(spec, turn_id="turn-1")

    assert harness.store.saved == []
    state = harness.manager.publication_state("ws_a")
    assert state.phase is PublicationPhase.FAILED
    assert state.published_projection_hash is None

    harness.port.verify_ready = True
    permit = await harness.manager.acquire_turn_permit(spec, turn_id="turn-2")

    assert permit.published_projection_hash == harness.store.published


@pytest.mark.asyncio
async def test_missing_config_isolation_blocks_publish_before_restart() -> None:
    harness = Harness()
    spec = _spec(isolated=False)
    await harness.manager.ensure_started(spec)

    with pytest.raises(RuntimePublishUnavailableError, match="not isolated"):
        await harness.manager.acquire_turn_permit(spec, turn_id="turn-1")

    assert harness.store.saved == []
    assert harness.sandbox.written == []
    assert len(harness.sandbox.started) == 1


@pytest.mark.asyncio
async def test_concurrent_acquires_share_one_publish() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)

    permits = await asyncio.gather(
        harness.manager.acquire_turn_permit(spec, turn_id="turn-1"),
        harness.manager.acquire_turn_permit(spec, turn_id="turn-2"),
        harness.manager.acquire_turn_permit(spec, turn_id="turn-3"),
    )

    assert len({permit.published_projection_hash for permit in permits}) == 1
    assert len(harness.port.verify_calls) == 1
    assert len(harness.renderer.renders) == 1
    assert len(harness.sandbox.started) == 2


@pytest.mark.asyncio
async def test_permit_requires_publication_ports() -> None:
    manager = WorkspaceRuntimeManager(port=FakePort(), sandbox=FakeSandbox())

    with pytest.raises(RuntimePublishUnavailableError, match="not configured"):
        await manager.acquire_turn_permit(_spec(), turn_id="turn-1")


@pytest.mark.asyncio
async def test_release_is_idempotent() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)
    permit = await harness.manager.acquire_turn_permit(spec, turn_id="turn-1")

    await harness.manager.release_turn_permit(permit)
    await harness.manager.release_turn_permit(permit)

    assert harness.manager.publication_state("ws_a").active_turns == 0
