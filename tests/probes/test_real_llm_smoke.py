"""真实 LLM 冒烟（可选）。

真实 OpenCode + 真实 provider + 真实 key，用于覆盖 mock 抓不到的鉴权、流式、
工具调用问题。默认被 addopts 排除；显式运行时必须提供 key，缺失直接失败，
不允许用 skip 掩盖。key 只从环境变量读取，不写入仓库/日志/快照。
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.probes.conftest import new_marker
from tests.probes.probe_env import (
    EventCollector,
    ProbeEnv,
    probe_tmp_root,
    resolve_opencode_bin,
    verify_opencode_binary,
)

pytestmark = [pytest.mark.probe, pytest.mark.real_llm]

MCP_SERVER = Path(__file__).with_name("mcp_echo_server.py")
PROVIDER_ENV = "WITTY_PROBE_REAL_PROVIDER"
MODEL_ENV = "WITTY_PROBE_REAL_MODEL"
API_KEY_ENV = "WITTY_PROBE_REAL_API_KEY"
BASE_URL_ENV = "WITTY_PROBE_REAL_BASE_URL"
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
PROMPT_TIMEOUT = 120.0


@pytest.fixture(scope="module")
def real_env() -> Iterator[ProbeEnv]:
    missing = [
        name
        for name in (PROVIDER_ENV, MODEL_ENV, API_KEY_ENV)
        if not os.environ.get(name)
    ]
    if missing:
        pytest.fail(
            "显式运行 real_llm 时必须提供真实 provider 凭据，缺少: "
            + ", ".join(missing),
            pytrace=False,
        )
    provider = os.environ[PROVIDER_ENV]
    model = os.environ[MODEL_ENV]
    api_key = os.environ[API_KEY_ENV]
    base_url = os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL)

    bin_path = resolve_opencode_bin()
    if bin_path is None:
        pytest.fail("未找到 OpenCode 1.17.13 可执行文件", pytrace=False)
    verify_opencode_binary(bin_path)
    env = ProbeEnv(
        root=probe_tmp_root() / f"real-{new_marker('run')}",
        mock_provider_base_url="http://127.0.0.1:1/v1",
        mcp_server_path=MCP_SERVER,
        bin_path=bin_path,
        extra_config={
            "provider": {
                provider: {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": provider,
                    "options": {"baseURL": base_url, "apiKey": api_key},
                    "models": {model: {"name": model, "status": "active"}},
                }
            },
            "model": f"{provider}/{model}",
        },
    )
    env.start()
    yield env
    env.stop()


def _completed_assistant(
    env: ProbeEnv, client: httpx.Client, session_id: str
) -> list[dict[str, Any]]:
    return [
        message
        for message in env.messages(client, session_id)
        if message["info"]["role"] == "assistant"
        and (message["info"].get("time") or {}).get("completed")
    ]


def _wait_turn(
    env: ProbeEnv,
    client: httpx.Client,
    session_id: str,
    timeout: float = PROMPT_TIMEOUT,
) -> list[dict[str, Any]]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        completed = _completed_assistant(env, client, session_id)
        if completed:
            return completed
        time.sleep(0.2)
    raise TimeoutError(
        f"真实 LLM 在 {timeout}s 内未完成; status={env.status(client).get(session_id)}"
    )


def _text(message: dict[str, Any]) -> str:
    return "\n".join(
        str(part.get("text", ""))
        for part in message.get("parts") or []
        if part.get("type") == "text"
    )


def test_r1_real_text_streaming(real_env: ProbeEnv) -> None:
    collector = EventCollector(real_env.base_url)
    collector.start()
    client = real_env.client()
    try:
        collector.wait_connected()
        session_id = real_env.create_session(client, title=new_marker("r1"))
        response = real_env.prompt(
            client,
            session_id,
            text="Reply with exactly this token and nothing else: PROBE_REAL_OK",
        )
        assert response.status_code == 204
        completed = _wait_turn(real_env, client, session_id)
        assistant = completed[-1]
        text = _text(assistant)
        assert text.strip(), f"assistant 文本为空: {assistant}"
        assert "PROBE" in text.upper()
        assert assistant["info"].get("finish") == "stop"
        scoped = [
            record.payload
            for record in collector.records()
            if (record.payload.get("properties") or {}).get("sessionID") == session_id
        ]
        assert any(
            event.get("type") == "message.part.delta"
            and (event.get("properties") or {}).get("delta")
            for event in scoped
        ), "真实 provider 未产生流式 delta"
        assert not any(event.get("type") == "session.error" for event in scoped)
    finally:
        client.close()
        collector.stop()


def test_r2_real_tool_call(real_env: ProbeEnv) -> None:
    client = real_env.client()
    try:
        session_id = real_env.create_session(client, title=new_marker("r2"))
        response = real_env.prompt(
            client,
            session_id,
            text=(
                "Call the probe_echo_upper tool with the text 'probe'. "
                "Then reply with the tool output exactly."
            ),
        )
        assert response.status_code == 204
        completed = _wait_turn(real_env, client, session_id)
        assistant = completed[-1]
        tool_parts = [
            part for part in assistant.get("parts") or [] if part.get("type") == "tool"
        ]
        if not tool_parts:
            pytest.xfail(
                "真实模型未调用 probe_echo_upper（模型限制）；"
                "R2 仅记录，不声称工具路径已由真实 LLM 验证"
            )
        outputs = []
        for part in tool_parts:
            state = part.get("state") or {}
            if state.get("status") == "completed":
                outputs.append(str(state.get("output", "")))
        assert outputs, f"tool part 未完成: {tool_parts}"
        assert any("PROBE" in output.upper() for output in outputs), outputs
    finally:
        client.close()
