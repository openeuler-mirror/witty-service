"""真实 dsh 进程验证 bridge patch 与 session 过滤，不需要 LLM 凭证。"""

from __future__ import annotations

import time
from pathlib import Path

import yaml
from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig, Notification

from witty_agent_server.infra.clients import dsh_client as dsh_client_module

_EMITTER = """
export const name = 'witty-test-emitter';

export function apply(ctx) {
  setTimeout(() => {
    const emit = (sessionId, header, text) => {
      ctx.emit('agent/assistant-stream', {
        agent: { session: { id: sessionId, header } },
        frame: { type: 'chunk', chunk: { type: 'text-delta', text } },
      });
    };
    emit('root-1', {}, 'root');
    emit('sub-1', { parentSession: 'root-1', origin: 'subagent', delegationDepth: 1 }, 'sub');
    emit('fork-1', { parentSession: 'root-1' }, 'fork');
  }, 300);
}
"""


def test_bridge_relays_root_and_fork_but_not_subagent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    emitter = tmp_path / "emitter.mjs"
    emitter.write_text(_EMITTER, encoding="utf-8")
    emitter_patch = tmp_path / "emitter.patch.yml"
    emitter_patch.write_text(
        yaml.safe_dump(
            [{"insert": [{"id": "witty-test-emitter", "name": emitter.as_uri()}]}],
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    harness = DeepSeekHarness(
        DeepSeekHarnessConfig(
            cwd=str(workspace),
            dsh_home=str(tmp_path / "dsh-home"),
            patches=(
                str(dsh_client_module._ASSISTANT_STREAM_PATCH),
                str(emitter_patch),
            ),
            initialize_timeout_seconds=60,
        )
    )
    subscription = harness.client.subscribe_notifications()
    streams: list[Notification] = []
    try:
        harness.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and len(streams) < 2:
            batch: list[Notification] = []
            subscription.drain(batch.append)
            streams.extend(
                item for item in batch if item.method == "witty.assistant.stream"
            )
            if len(streams) < 2:
                time.sleep(0.05)
    finally:
        subscription.close()
        harness.close()

    assert {item.payload.get("sessionId") for item in streams} == {
        "root-1",
        "fork-1",
    }
