"""R06 location 预热与可见性探针。

运行：``WITTY_PROBE_OPENCODE_DIR=<dir> pytest tests/probes/test_opencode_v2_location.py -m probe -q``
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.probes.probe_env import ProbeEnv
from witty_agent_server.infra.clients.opencode_v2_client import OpenCodeV2Client
from witty_agent_server.runtime_adapter import OpenCodeRuntimeAdapter
from witty_service.runtime.contracts import (
    RuntimeConnection,
    RuntimeLocationVerification,
    RuntimeModelRef,
    RuntimeWorkspace,
)

pytestmark = pytest.mark.probe

_WORKSPACE_ID = "ws_probe_location"
_MODEL = RuntimeModelRef(provider_id="mock", model_id="mock-model")


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


async def _run(env: ProbeEnv) -> None:
    await _warm_location(OpenCodeV2Client(env.base_url), str(env.work_dir))
    adapter = OpenCodeRuntimeAdapter()
    workspace = RuntimeWorkspace(
        workspace_id=_WORKSPACE_ID, runtime_directory=str(env.work_dir)
    )
    await adapter.connect(RuntimeConnection(workspace=workspace, base_url=env.base_url))
    try:
        verified = await adapter.verify_location(
            RuntimeLocationVerification(
                workspace_id=_WORKSPACE_ID,
                agents=("probe-a", "probe-b"),
                models=(_MODEL,),
                skills=("probe-skill",),
                timeout=30.0,
            )
        )
        assert verified.ready, verified.detail
        assert {"probe-a", "probe-b"} <= set(verified.agents)
        assert _MODEL in verified.models
        assert "probe-skill" in verified.skills

        missing = await adapter.verify_location(
            RuntimeLocationVerification(
                workspace_id=_WORKSPACE_ID, agents=("missing-agent",)
            )
        )
        assert not missing.ready
        assert "missing-agent" in (missing.detail or "")
    finally:
        await adapter.disconnect(_WORKSPACE_ID)


def test_real_v2_location_verification(env: ProbeEnv) -> None:
    asyncio.run(_run(env))
