from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping

import pytest

from witty_service.runtime.contracts import (
    ReplayQuery,
    RuntimeConnection,
    RuntimeEmptyPayload,
    RuntimeErrorPayload,
    RuntimeEventEnvelope,
    RuntimeEventReplay,
    RuntimeEventType,
    RuntimeHealth,
    RuntimeLocationReadiness,
    RuntimeLocationVerification,
    RuntimeMessageCompletedPayload,
    RuntimeMessageDeltaPayload,
    RuntimeWorkspace,
)
from witty_service.runtime.errors import RuntimeUnavailableError
from witty_service.runtime.events import (
    RuntimeResyncReason,
    RuntimeResyncRequired,
    RuntimeTurnEvent,
)
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
from witty_service.sandbox.base import (
    AdapterEndpoint,
    SandboxBackend,
    SandboxHandle,
    SandboxStatus,
)


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
    def __init__(self) -> None:
        self.published: str | None = None

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


_END = object()


class FakePort:
    def __init__(self) -> None:
        self.streams: dict[str, list[asyncio.Queue[object]]] = {}
        self.replay_handler: Callable[[ReplayQuery], RuntimeEventReplay] | None = None
        self.replay_queries: list[ReplayQuery] = []
        self.health_ready = True

    async def connect(self, connection: RuntimeConnection) -> RuntimeHealth:
        return RuntimeHealth(workspace_id=connection.workspace.workspace_id, ready=True)

    async def disconnect(self, workspace_id: str) -> None:
        return None

    async def health(self, workspace_id: str) -> RuntimeHealth:
        return RuntimeHealth(
            workspace_id=workspace_id,
            ready=self.health_ready,
            detail=None if self.health_ready else "runtime is down",
        )

    def subscribe(self, workspace_id: str) -> AsyncIterator[RuntimeEventEnvelope]:
        queue: asyncio.Queue[object] = asyncio.Queue()
        self.streams.setdefault(workspace_id, []).append(queue)
        return self._stream(workspace_id, queue)

    async def _stream(
        self, workspace_id: str, queue: asyncio.Queue[object]
    ) -> AsyncIterator[RuntimeEventEnvelope]:
        try:
            while True:
                item = await queue.get()
                if item is _END:
                    return
                if isinstance(item, Exception):
                    raise item
                assert isinstance(item, RuntimeEventEnvelope)
                yield item
        finally:
            self.streams[workspace_id].remove(queue)

    async def replay_events(self, query: ReplayQuery) -> RuntimeEventReplay:
        self.replay_queries.append(query)
        if self.replay_handler is None:
            return RuntimeEventReplay(events=(), has_more=False)
        return self.replay_handler(query)

    async def verify_location(
        self, request: RuntimeLocationVerification
    ) -> RuntimeLocationReadiness:
        return RuntimeLocationReadiness(
            workspace_id=request.workspace_id,
            ready=True,
            agents=request.agents,
            models=request.models,
            skills=request.skills,
        )

    def emit(self, workspace_id: str, item: object) -> None:
        self.streams[workspace_id][-1].put_nowait(item)

    def end_stream(self, workspace_id: str, error: Exception | None = None) -> None:
        self.emit(workspace_id, error if error is not None else _END)

    async def wait_for_stream(self, workspace_id: str) -> None:
        while not self.streams.get(workspace_id):
            await asyncio.sleep(0)


class FakeSandbox(SandboxBackend):
    sandbox_type = "fake"

    def start(
        self,
        *,
        workspace_id: str,
        workspace_path: str,
        env: dict[str, str] | None = None,
        **kwargs: object,
    ) -> SandboxHandle:
        return SandboxHandle(
            sandbox_id=f"sbx-{workspace_id}",
            workspace_id=workspace_id,
            workspace_path=workspace_path,
            metadata={"base_url": f"http://127.0.0.1:4100/{workspace_id}"},
        )

    def stop(self, handle: SandboxHandle | str, **kwargs: object) -> None:
        return None

    def status(self, handle: SandboxHandle | str, **kwargs: object) -> SandboxStatus:
        return SandboxStatus.running

    def endpoint(
        self, handle: SandboxHandle | str, **kwargs: object
    ) -> AdapterEndpoint:
        assert isinstance(handle, SandboxHandle)
        return AdapterEndpoint(base_url=str(handle.metadata["base_url"]))

    def cleanup(self, handle: SandboxHandle | str, **kwargs: object) -> None:
        return None

    def write_runtime_files(
        self, *, runtime_home: str, files: Mapping[str, str]
    ) -> None:
        return None


def _spec(workspace_id: str = "ws_a") -> WorkspaceRuntimeSpec:
    home = f"/srv/{workspace_id}/runtime"
    return WorkspaceRuntimeSpec(
        workspace=RuntimeWorkspace(
            workspace_id=workspace_id, runtime_directory=f"/srv/{workspace_id}"
        ),
        host_directory=f"/srv/{workspace_id}",
        command=("opencode", "serve", "--port", "{port}"),
        env={"HOME": home, "XDG_CONFIG_HOME": f"{home}/config"},
        preferred_port=4100,
    )


class Harness:
    def __init__(
        self,
        *,
        subscriber_queue_size: int = 64,
        subscriber_timeout: float = 5.0,
    ) -> None:
        self.port = FakePort()
        self.manager = WorkspaceRuntimeManager(
            port=self.port,
            sandbox=FakeSandbox(),
            projection_source=FakeProjectionSource(),
            projection_store=FakeProjectionStore(),
            projection_renderer=FakeRenderer(),
            credential_resolver=FakeCredentials(),
            publish_timeout=5.0,
            subscriber_queue_size=subscriber_queue_size,
            subscriber_timeout=subscriber_timeout,
            event_reconnect_delay=0.01,
        )


def _envelope(
    event_type: RuntimeEventType,
    *,
    seq: int | None,
    session: str = "ses_1",
    workspace: str = "ws_a",
) -> RuntimeEventEnvelope:
    payloads = {
        RuntimeEventType.MESSAGE_DELTA: RuntimeMessageDeltaPayload(delta="chunk"),
        RuntimeEventType.MESSAGE_COMPLETED: RuntimeMessageCompletedPayload(),
        RuntimeEventType.SESSION_ERROR: RuntimeErrorPayload(
            code="session_error", message="boom"
        ),
    }
    return RuntimeEventEnvelope(
        workspace_id=workspace,
        runtime_session_id=session,
        event_id=f"evt_{session}_{seq}_{event_type.value}",
        type=event_type,
        durable_seq=seq,
        occurred_at=0,
        payload=payloads.get(event_type, RuntimeEmptyPayload()),
    )


async def _collect(
    manager: WorkspaceRuntimeManager, workspace_id: str, items: list
) -> None:
    async for item in manager.subscribe_events(workspace_id):
        items.append(item)


async def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition was not reached in time")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_events_are_attributed_to_active_turn() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)
    await harness.manager.acquire_turn_permit(
        spec, turn_id="turn-1", runtime_session_id="ses_1"
    )
    await harness.port.wait_for_stream("ws_a")
    items: list[RuntimeTurnEvent | RuntimeResyncRequired] = []
    consumer = asyncio.create_task(_collect(harness.manager, "ws_a", items))
    try:
        harness.port.emit("ws_a", _envelope(RuntimeEventType.MESSAGE_DELTA, seq=None))
        harness.port.emit("ws_a", _envelope(RuntimeEventType.TURN_COMPLETED, seq=2))
        await _wait_until(lambda: len(items) >= 2)

        assert [item.event.type for item in items] == [
            RuntimeEventType.MESSAGE_DELTA,
            RuntimeEventType.TURN_COMPLETED,
        ]
        assert all(item.turn_id == "turn-1" for item in items)

        harness.port.emit("ws_a", _envelope(RuntimeEventType.MESSAGE_DELTA, seq=None))
        harness.port.emit(
            "ws_a", _envelope(RuntimeEventType.TURN_STARTED, seq=None, session="ses_x")
        )
        await asyncio.sleep(0.05)
        assert len(items) == 2

        harness.port.emit(
            "ws_a", _envelope(RuntimeEventType.SESSION_IDLE, seq=None, session="ses_x")
        )
        await _wait_until(lambda: len(items) == 3)
        assert items[2].turn_id is None
    finally:
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_reconnect_replays_gap_without_duplicates() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)
    await harness.port.wait_for_stream("ws_a")
    items: list[RuntimeTurnEvent | RuntimeResyncRequired] = []
    consumer = asyncio.create_task(_collect(harness.manager, "ws_a", items))
    try:
        harness.port.emit("ws_a", _envelope(RuntimeEventType.TURN_STARTED, seq=1))
        await _wait_until(lambda: len(items) == 1)

        harness.port.replay_handler = lambda query: RuntimeEventReplay(
            events=(
                _envelope(RuntimeEventType.TURN_STARTED, seq=1),
                _envelope(RuntimeEventType.MESSAGE_STARTED, seq=2),
            ),
            has_more=False,
        )
        harness.port.end_stream(
            "ws_a", RuntimeUnavailableError(workspace_id="ws_a", detail="reset")
        )
        await _wait_until(
            lambda: any(isinstance(i, RuntimeResyncRequired) for i in items)
        )
        await _wait_until(lambda: len(items) == 3)

        harness.port.emit("ws_a", _envelope(RuntimeEventType.MESSAGE_STARTED, seq=2))
        harness.port.emit("ws_a", _envelope(RuntimeEventType.MESSAGE_COMPLETED, seq=3))
        await _wait_until(lambda: len(items) == 4)
        await asyncio.sleep(0.05)

        assert [
            item.event.durable_seq
            for item in items
            if isinstance(item, RuntimeTurnEvent)
        ] == [1, 2, 3]
        resync = next(i for i in items if isinstance(i, RuntimeResyncRequired))
        assert resync.reason == "event_stream_interrupted"
        assert resync.last_durable_seq == 1
        assert harness.port.replay_queries[0].after_seq == 1
    finally:
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_two_sessions_do_not_cross() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)
    await harness.manager.acquire_turn_permit(
        spec, turn_id="turn-1", runtime_session_id="ses_1"
    )
    await harness.manager.acquire_turn_permit(
        spec, turn_id="turn-2", runtime_session_id="ses_2"
    )
    await harness.port.wait_for_stream("ws_a")
    items: list[RuntimeTurnEvent | RuntimeResyncRequired] = []
    consumer = asyncio.create_task(_collect(harness.manager, "ws_a", items))
    try:
        harness.port.emit(
            "ws_a", _envelope(RuntimeEventType.TURN_STARTED, seq=1, session="ses_2")
        )
        harness.port.emit(
            "ws_a", _envelope(RuntimeEventType.TURN_STARTED, seq=1, session="ses_1")
        )
        await _wait_until(lambda: len(items) == 2)

        by_session = {item.event.runtime_session_id: item.turn_id for item in items}
        assert by_session == {"ses_1": "turn-1", "ses_2": "turn-2"}
    finally:
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_slow_subscriber_gets_resync_and_is_closed() -> None:
    harness = Harness(subscriber_queue_size=1, subscriber_timeout=0.05)
    spec = _spec()
    await harness.manager.ensure_started(spec)
    await harness.port.wait_for_stream("ws_a")
    stream = harness.manager.subscribe_events("ws_a")
    first = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    harness.port.emit("ws_a", _envelope(RuntimeEventType.TURN_STARTED, seq=1))
    assert isinstance(await asyncio.wait_for(first, timeout=1), RuntimeTurnEvent)

    harness.port.emit("ws_a", _envelope(RuntimeEventType.MESSAGE_STARTED, seq=2))
    harness.port.emit("ws_a", _envelope(RuntimeEventType.MESSAGE_COMPLETED, seq=3))
    state = await asyncio.wait_for(harness.manager.health("ws_a"), timeout=1)
    assert state.phase is RuntimePhase.HEALTHY
    await asyncio.sleep(0.15)

    item = await asyncio.wait_for(stream.__anext__(), timeout=1)
    assert isinstance(item, RuntimeResyncRequired)
    assert item.reason is RuntimeResyncReason.SUBSCRIBER_LAGGED
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), timeout=1)


@pytest.mark.asyncio
async def test_delta_overflow_closes_subscriber_with_resync() -> None:
    harness = Harness(subscriber_queue_size=1, subscriber_timeout=0.05)
    spec = _spec()
    await harness.manager.ensure_started(spec)
    await harness.manager.acquire_turn_permit(
        spec, turn_id="turn-1", runtime_session_id="ses_1"
    )
    await harness.port.wait_for_stream("ws_a")
    stream = harness.manager.subscribe_events("ws_a")
    first = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    harness.port.emit(
        "ws_a", _envelope(RuntimeEventType.TURN_STARTED, seq=1, session="ses_1")
    )
    assert isinstance(await asyncio.wait_for(first, timeout=1), RuntimeTurnEvent)

    harness.port.emit(
        "ws_a", _envelope(RuntimeEventType.MESSAGE_DELTA, seq=None, session="ses_1")
    )
    harness.port.emit(
        "ws_a", _envelope(RuntimeEventType.MESSAGE_DELTA, seq=None, session="ses_1")
    )
    await asyncio.sleep(0.05)

    item = await asyncio.wait_for(stream.__anext__(), timeout=1)
    assert isinstance(item, RuntimeResyncRequired)
    assert item.reason is RuntimeResyncReason.SUBSCRIBER_LAGGED
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), timeout=1)


@pytest.mark.asyncio
async def test_reconnect_replays_active_session_with_pagination() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)
    await harness.manager.acquire_turn_permit(
        spec, turn_id="turn-1", runtime_session_id="ses_1"
    )
    await harness.port.wait_for_stream("ws_a")
    items: list[RuntimeTurnEvent | RuntimeResyncRequired] = []
    consumer = asyncio.create_task(_collect(harness.manager, "ws_a", items))
    try:

        def replay(query: ReplayQuery) -> RuntimeEventReplay:
            if query.after_seq == 0:
                return RuntimeEventReplay(
                    events=(
                        _envelope(RuntimeEventType.TURN_STARTED, seq=1),
                        _envelope(RuntimeEventType.MESSAGE_STARTED, seq=2),
                    ),
                    has_more=True,
                )
            return RuntimeEventReplay(
                events=(_envelope(RuntimeEventType.TURN_COMPLETED, seq=3),),
                has_more=False,
            )

        harness.port.replay_handler = replay
        harness.port.end_stream(
            "ws_a", RuntimeUnavailableError(workspace_id="ws_a", detail="reset")
        )
        await _wait_until(
            lambda: len([i for i in items if isinstance(i, RuntimeTurnEvent)]) == 3
        )
        assert [query.after_seq for query in harness.port.replay_queries] == [0, 2]
        replayed = [i for i in items if isinstance(i, RuntimeTurnEvent)]
        assert [item.event.durable_seq for item in replayed] == [1, 2, 3]
        assert all(item.turn_id == "turn-1" for item in replayed)
    finally:
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer


@pytest.mark.asyncio
async def test_shutdown_closes_subscribers() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)
    stream = harness.manager.subscribe_events("ws_a")
    pending = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)

    await harness.manager.shutdown()

    item = await asyncio.wait_for(pending, timeout=1)
    assert isinstance(item, RuntimeResyncRequired)
    assert item.reason is RuntimeResyncReason.RUNTIME_STOPPED
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(stream.__anext__(), timeout=1)


@pytest.mark.asyncio
async def test_restart_rebuilds_chain_and_requires_resync() -> None:
    harness = Harness()
    spec = _spec()
    await harness.manager.ensure_started(spec)
    await harness.port.wait_for_stream("ws_a")
    items: list[RuntimeTurnEvent | RuntimeResyncRequired] = []
    consumer = asyncio.create_task(_collect(harness.manager, "ws_a", items))
    try:
        await harness.manager.restart(spec)
        await _wait_until(
            lambda: any(isinstance(i, RuntimeResyncRequired) for i in items)
        )
        resync = next(i for i in items if isinstance(i, RuntimeResyncRequired))
        assert resync.reason == "runtime_restarted"

        await harness.port.wait_for_stream("ws_a")
        harness.port.emit("ws_a", _envelope(RuntimeEventType.SESSION_IDLE, seq=1))
        await _wait_until(
            lambda: any(
                isinstance(i, RuntimeTurnEvent)
                and i.event.type is RuntimeEventType.SESSION_IDLE
                for i in items
            )
        )
    finally:
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
