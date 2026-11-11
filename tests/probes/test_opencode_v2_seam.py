"""R04 垂直链路探针。

运行：``WITTY_PROBE_OPENCODE_DIR=<dir> pytest tests/probes/test_opencode_v2_seam.py -m probe -q``
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.probes.mock_provider import MockProvider
from tests.probes.probe_env import ProbeEnv
from witty_agent_server.infra.clients.opencode_v2_client import OpenCodeV2Client
from witty_agent_server.runtime_adapter import OpenCodeRuntimeAdapter
from witty_service.runtime.contracts import (
    EnsureSessionRequest,
    HistoryQuery,
    ReplayQuery,
    RuntimeConnection,
    RuntimeEventEnvelope,
    RuntimeEventType,
    RuntimeMessageKind,
    RuntimeModelRef,
    RuntimeTextPart,
    RuntimeWorkspace,
    TurnExecutionRequest,
)

pytestmark = pytest.mark.probe

_MODEL = RuntimeModelRef(provider_id="mock", model_id="mock-model")
_USER_TEXT = "PROBE_SEAM_VERTICAL"


async def _warm_location(client: OpenCodeV2Client, directory: str) -> None:
    created = await client.create_session(directory=directory)
    session_id = str(created["id"])
    for _ in range(60):
        await client.prompt(
            session_id,
            message_id=f"msg_warm_{uuid.uuid4().hex[:8]}",
            text="warm up",
        )
        events, _ = await client.history(session_id, after_seq=0, limit=100)
        if any(event.get("type") == "session.next.step.started" for event in events):
            return
        await asyncio.sleep(0.5)
    raise AssertionError("location 在 30s 内没有进入可执行状态")


async def _run(env: ProbeEnv, mock: MockProvider) -> None:
    client = OpenCodeV2Client(env.base_url)
    await _warm_location(client, str(env.work_dir))

    adapter = OpenCodeRuntimeAdapter()
    workspace = RuntimeWorkspace(
        workspace_id="ws_probe", runtime_directory=str(env.work_dir)
    )
    await adapter.connect(RuntimeConnection(workspace=workspace, base_url=env.base_url))
    session = await adapter.ensure_session(
        EnsureSessionRequest(workspace=workspace, session_id="session_probe")
    )

    live: list[RuntimeEventEnvelope] = []

    async def consume() -> None:
        async for envelope in adapter.subscribe("ws_probe"):
            if envelope.runtime_session_id != session.runtime_session_id:
                continue
            live.append(envelope)
            if envelope.type in {
                RuntimeEventType.TURN_COMPLETED,
                RuntimeEventType.TURN_FAILED,
                RuntimeEventType.SESSION_ERROR,
            }:
                return

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.5)

    request = TurnExecutionRequest(
        workspace_id="ws_probe",
        session_id="session_probe",
        turn_id="turn_probe",
        runtime_session_id=session.runtime_session_id,
        message_id="msg_probe_1",
        agent_internal_name="probe-a",
        model=_MODEL,
        parts=(RuntimeTextPart(text=_USER_TEXT),),
    )
    admission = await adapter.admit_turn(request)
    assert admission.message_id == "msg_probe_1"
    assert admission.runtime_session_id == session.runtime_session_id
    assert admission.admitted_seq > 0

    await asyncio.wait_for(consumer, timeout=60)
    live_types = {envelope.type for envelope in live}
    assert RuntimeEventType.MESSAGE_DELTA in live_types
    assert RuntimeEventType.TURN_COMPLETED in live_types
    assert all(
        envelope.durable_seq is None
        for envelope in live
        if envelope.type is RuntimeEventType.MESSAGE_DELTA
    )

    replay = await adapter.replay_events(
        ReplayQuery(
            workspace_id="ws_probe",
            runtime_session_id=session.runtime_session_id,
            after_seq=0,
            limit=100,
        )
    )
    replay_types = {event.type for event in replay.events}
    assert RuntimeEventType.TURN_STARTED in replay_types
    assert RuntimeEventType.TURN_COMPLETED in replay_types
    assert RuntimeEventType.MESSAGE_DELTA not in replay_types

    page = await adapter.read_history(
        HistoryQuery(
            workspace_id="ws_probe",
            runtime_session_id=session.runtime_session_id,
            limit=20,
        )
    )
    user_texts = [
        part.text
        for item in page.items
        if item.kind is RuntimeMessageKind.USER
        for part in item.parts
    ]
    assistant_texts = [
        part.text
        for item in page.items
        if item.kind is RuntimeMessageKind.ASSISTANT
        for part in item.parts
    ]
    assert _USER_TEXT in user_texts
    assert any(assistant_texts)

    provider_calls = len(mock.snapshot())
    await adapter.admit_turn(request)
    await asyncio.sleep(1)
    assert len(mock.snapshot()) == provider_calls


def test_real_v2_seam_vertical(env: ProbeEnv, mock: MockProvider) -> None:
    asyncio.run(_run(env, mock))
