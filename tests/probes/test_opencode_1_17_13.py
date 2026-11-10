"""OpenCode 1.17.13 legacy 接口（``/session``、``/event``）能力探针。

断言 OpenCode 的真实行为而非文档转述：provider 实际收到的 system prompt、
tools、model、history（由 ``MockProvider`` 记录），以及 session、message、
event 的真实形态和重启前后的一致性。

每个用例使用独立 session + 唯一 marker；断言按 marker 锚定，不依赖请求顺序。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.probes.conftest import new_marker
from tests.probes.mock_provider import MockProvider
from tests.probes.probe_env import (
    AGENT_A_PROMPT,
    AGENT_B_PROMPT,
    SKILL_BODY,
    SKILL_NAME,
    EventCollector,
    ProbeEnv,
)

pytestmark = pytest.mark.probe

MCP_TOOL_NAMES = {"probe_echo_upper", "probe_echo_lower", "probe_slow_echo"}
# 旧 MCP 原始工具名 / 原始 server 名不应直接暴露给 provider。
RAW_MCP_NAMES = {"echo_upper", "echo_lower", "slow.echo", "slow_echo"}


# --------------------------------------------------------------------------- helpers
def system_text(body: dict[str, Any]) -> str:
    return "\n".join(
        str(message.get("content", ""))
        for message in body.get("messages") or []
        if message.get("role") == "system"
    )


def provider_texts(body: dict[str, Any]) -> list[str]:
    return [
        str(message.get("content", ""))
        for message in body.get("messages") or []
        if message.get("role") in {"user", "assistant"}
    ]


def message_text(message: dict[str, Any]) -> str:
    return "\n".join(
        str(part.get("text", ""))
        for part in message.get("parts") or []
        if part.get("type") == "text"
    )


def message_roles(messages: list[dict[str, Any]]) -> list[str]:
    return [str(message["info"]["role"]) for message in messages]


def event_error_text(event: dict[str, Any]) -> str:
    properties = event.get("properties") or {}
    error = properties.get("error")
    if isinstance(error, dict):
        data = error.get("data") or {}
        message = data.get("message") if isinstance(data, dict) else None
        return str(message or json.dumps(error, ensure_ascii=False))
    return str(error or event)


def session_revert_id(session: dict[str, Any]) -> str | None:
    revert = session.get("revert")
    if isinstance(revert, dict):
        return revert.get("messageID")  # type: ignore[return-value]
    return revert  # type: ignore[return-value]


def new_session(
    env: ProbeEnv,
    client: httpx.Client,
    prefix: str,
    *,
    directory: Path | None = None,
) -> tuple[str, str]:
    marker = new_marker(prefix)
    session_id = env.create_session(client, directory=directory, title=marker)
    return session_id, marker


def assert_tools(
    mock: MockProvider,
    marker: str,
    expected: set[str],
    context: str,
) -> set[str]:
    actual = set(mock.tool_names_for(marker))
    assert actual == expected, (
        f"{context}: provider 实际工具集合与期望不一致\n"
        f"  actual  ={sorted(actual)}\n"
        f"  expected={sorted(expected)}"
    )
    return actual


def call_provider_tool(
    env: ProbeEnv,
    client: httpx.Client,
    mock: MockProvider,
    session_id: str,
    tool: str,
    *,
    tail: str = "",
    prefix: str = "tool",
) -> tuple[str, list[str]]:
    marker = new_marker(prefix)
    text = " ".join(part for part in ("CALLTOOL", tool, tail, marker) if part)
    env.prompt_and_wait(client, session_id, text=text)
    mock.wait_for_marker(marker, count=2, timeout=30)
    return marker, mock.tool_results_for(marker)


def wait_status_busy(
    env: ProbeEnv, client: httpx.Client, session_id: str, timeout: float = 30.0
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if env.status(client).get(session_id):
            return
        time.sleep(0.05)
    raise TimeoutError(f"session {session_id} never became busy; {env.log_tail()}")


# ------------------------------------------------------ 版本 / Agent / model / 工具集
def test_p01_version_and_package_identity(env: ProbeEnv, client: httpx.Client) -> None:
    info = env.binary_info
    assert info is not None
    assert info.version == "1.17.13"
    assert info.package_name == "opencode-ai"
    assert info.package_version == "1.17.13"
    health = client.get("/global/health")
    health.raise_for_status()
    assert health.json()["version"] == "1.17.13"
    assert env.health_version() == "1.17.13"


def test_p02_per_turn_agent(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_id, _ = new_session(env, client, "p02")

    marker_a = new_marker("p02-a")
    env.prompt_and_wait(client, session_id, text=marker_a, agent="probe-a")
    system_a = system_text(mock.request_for(marker_a))
    assert AGENT_A_PROMPT in system_a
    assert AGENT_B_PROMPT not in system_a

    marker_b = new_marker("p02-b")
    env.prompt_and_wait(client, session_id, text=marker_b, agent="probe-b")
    system_b = system_text(mock.request_for(marker_b))
    assert AGENT_B_PROMPT in system_b
    assert AGENT_A_PROMPT not in system_b

    marker_build = new_marker("p02-build")
    env.prompt_and_wait(client, session_id, text=marker_build)
    system_build = system_text(mock.request_for(marker_build))
    assert AGENT_A_PROMPT not in system_build
    assert AGENT_B_PROMPT not in system_build
    assert "You are opencode" in system_build


def test_p03_per_turn_model(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_id, _ = new_session(env, client, "p03")

    marker_b = new_marker("p03-b")
    env.prompt_and_wait(
        client, session_id, text=marker_b, model=("mock", "mock-model-b")
    )
    assert mock.request_for(marker_b)["model"] == "mock-model-b"

    marker_sticky = new_marker("p03-sticky")
    env.prompt_and_wait(client, session_id, text=marker_sticky)
    assert mock.request_for(marker_sticky)["model"] == "mock-model-b"

    marker_default = new_marker("p03-default")
    env.prompt_and_wait(
        client, session_id, text=marker_default, model=("mock", "mock-model")
    )
    assert mock.request_for(marker_default)["model"] == "mock-model"
    session = env.session(client, session_id)
    assert session["model"]["id"] == "mock-model"

    # 新 session 不应继承旧 session 的显式 model。
    other_id, _ = new_session(env, client, "p03-other")
    marker_other = new_marker("p03-other-b")
    env.prompt_and_wait(
        client, other_id, text=marker_other, model=("mock", "mock-model-b")
    )
    assert mock.request_for(marker_other)["model"] == "mock-model-b"


def test_p04_per_turn_directory(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    token_a = f"PROBE_AGENTS_A_{uuid.uuid4().hex[:8]}"
    token_b = f"PROBE_AGENTS_B_{uuid.uuid4().hex[:8]}"
    dir_a = env.work_dir / "dir-a"
    dir_b = env.work_dir / "dir-b"
    dir_a.mkdir(parents=True, exist_ok=True)
    dir_b.mkdir(parents=True, exist_ok=True)
    (dir_a / "AGENTS.md").write_text(f"# Directory A\n\n{token_a}\n", encoding="utf-8")
    (dir_b / "AGENTS.md").write_text(f"# Directory B\n\n{token_b}\n", encoding="utf-8")

    session_a, marker_a = new_session(env, client, "p04-a", directory=dir_a)
    env.prompt_and_wait(client, session_a, text=marker_a, directory=dir_a)
    body_a = mock.request_for(marker_a)
    assert token_a in system_text(body_a)
    assert token_b not in system_text(body_a)
    assert env.session(client, session_a)["directory"] == str(dir_a)

    session_b, marker_b = new_session(env, client, "p04-b", directory=dir_b)
    env.prompt_and_wait(client, session_b, text=marker_b, directory=dir_b)
    body_b = mock.request_for(marker_b)
    assert token_b in system_text(body_b)
    assert token_a not in system_text(body_b)
    assert env.session(client, session_b)["directory"] == str(dir_b)

    # directory 是逐轮定位参数：带错目录时要么被拒绝，要么仍按 session 自身
    # 目录解析，不能把 B 的 AGENTS.md 串进 A。
    wrong_marker = new_marker("p04-wrong")
    response = env.prompt(client, session_a, text=wrong_marker, directory=dir_b)
    if response.status_code in {400, 404, 500}:
        assert mock.count_for(wrong_marker) == 0
    else:
        assert response.status_code == 204
        mock.wait_for_marker(wrong_marker, count=1, timeout=30)
        wrong_system = system_text(mock.request_for(wrong_marker))
        assert token_a in wrong_system
        assert token_b not in wrong_system


def test_p05_default_toolset_and_sanitize(
    env: ProbeEnv,
    client: httpx.Client,
    mock: MockProvider,
    baseline_tools: set[str],
) -> None:
    session_id, marker = new_session(env, client, "p05")
    env.prompt_and_wait(client, session_id, text=marker)
    actual = assert_tools(mock, marker, baseline_tools, "默认工具集")
    assert actual >= MCP_TOOL_NAMES
    assert not (RAW_MCP_NAMES & actual)
    assert "skill" in actual


# --------------------------------------------------- tools 粘滞 / MCP / Skill / revert
def test_p06_tools_map_stickiness(
    env: ProbeEnv,
    client: httpx.Client,
    mock: MockProvider,
    baseline_tools: set[str],
) -> None:
    session_id, _ = new_session(env, client, "p06")
    upper = "probe_echo_upper"

    marker_off = new_marker("p06-off")
    env.prompt_and_wait(client, session_id, text=marker_off, tools={upper: False})
    assert_tools(mock, marker_off, baseline_tools - {upper}, "禁用后")

    marker_sticky = new_marker("p06-sticky")
    env.prompt_and_wait(client, session_id, text=marker_sticky)
    assert_tools(mock, marker_sticky, baseline_tools - {upper}, "省略 tools 后仍禁用")

    marker_on = new_marker("p06-on")
    env.prompt_and_wait(client, session_id, text=marker_on, tools={upper: True})
    assert_tools(mock, marker_on, baseline_tools, "显式 true 恢复")

    other_id, _ = new_session(env, client, "p06-other")
    marker_other = new_marker("p06-other")
    env.prompt_and_wait(client, other_id, text=marker_other)
    assert_tools(mock, marker_other, baseline_tools, "新 session 全量")


def test_p07_mcp_roundtrip(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    status = env.mcp_status(client)
    assert status["probe"]["status"] == "connected"
    session_id, _ = new_session(env, client, "p07")
    marker, results = call_provider_tool(
        env, client, mock, session_id, "probe_echo_upper", prefix="p07"
    )
    assert marker
    assert any("PROBE" in result.upper() for result in results), results


def test_p08_skill_exposure(
    env: ProbeEnv,
    client: httpx.Client,
    mock: MockProvider,
    baseline_tools: set[str],
) -> None:
    skills = env.skills(client)
    assert any(skill.get("name") == SKILL_NAME for skill in skills)

    session_id, _ = new_session(env, client, "p08")
    marker, results = call_provider_tool(
        env, client, mock, session_id, "skill", prefix="p08-skill"
    )
    assert marker
    assert any(SKILL_BODY in result for result in results), results

    marker_off = new_marker("p08-off")
    env.prompt_and_wait(client, session_id, text=marker_off, tools={"skill": False})
    actual = assert_tools(mock, marker_off, baseline_tools - {"skill"}, "skill:false")
    assert "skill" not in actual


def test_p09_revert_cuts_history(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_id, _ = new_session(env, client, "p09")
    marker_1 = new_marker("p09-1")
    marker_2 = new_marker("p09-2")
    env.prompt_and_wait(client, session_id, text=marker_1)
    env.prompt_and_wait(client, session_id, text=marker_2)
    user_ids = env.user_message_ids(client, session_id)
    assert len(user_ids) == 2

    env.revert(client, session_id, user_ids[1])
    session = env.session(client, session_id)
    assert session_revert_id(session) == user_ids[1]
    assert len(env.messages(client, session_id)) == 4

    marker_3 = new_marker("p09-3")
    env.prompt_and_wait(client, session_id, text=marker_3)
    history = provider_texts(mock.request_for(marker_3))
    joined = "\n".join(history)
    assert marker_1 in joined
    assert marker_2 not in joined
    assert marker_3 in joined
    assert session_revert_id(env.session(client, session_id)) is None


def test_p10_unrevert_restores_history(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_id, _ = new_session(env, client, "p10")
    marker_1 = new_marker("p10-1")
    marker_2 = new_marker("p10-2")
    env.prompt_and_wait(client, session_id, text=marker_1)
    env.prompt_and_wait(client, session_id, text=marker_2)
    user_ids = env.user_message_ids(client, session_id)

    env.revert(client, session_id, user_ids[1])
    env.unrevert(client, session_id)
    assert session_revert_id(env.session(client, session_id)) is None
    assert len(env.messages(client, session_id)) == 4

    marker_3 = new_marker("p10-3")
    env.prompt_and_wait(client, session_id, text=marker_3)
    joined = "\n".join(provider_texts(mock.request_for(marker_3)))
    assert marker_1 in joined
    assert marker_2 in joined
    assert marker_3 in joined


# --------------------------------------------------------------- abort / 事件流 / 重启
def test_p11_abort_keeps_interrupted_message(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    collector = EventCollector(env.base_url)
    collector.start()
    try:
        collector.wait_connected()
        session_id, _ = new_session(env, client, "p11")
        marker = new_marker("p11")
        response = env.prompt(client, session_id, text=f"SLOW {marker}")
        response.raise_for_status()
        mock.wait_for_marker(marker, count=1, timeout=30)
        wait_status_busy(env, client, session_id)
        deadline = time.time() + 20
        while time.time() < deadline:
            if any(
                message["info"]["role"] == "assistant"
                for message in env.messages(client, session_id)
            ):
                break
            time.sleep(0.05)
        else:
            raise TimeoutError("assistant 消息在 abort 前没有出现")

        env.abort_and_wait(client, session_id)
        error_event = collector.wait_for_type(
            "session.error", session_id=session_id, timeout=20
        )
        assert "abort" in event_error_text(error_event).lower()

        messages = env.messages(client, session_id)
        assert any(
            message["info"]["role"] == "user" and marker in message_text(message)
            for message in messages
        )
        aborted = [
            message
            for message in messages
            if message["info"]["role"] == "assistant"
            and "aborted"
            in str((message["info"].get("error") or {}).get("name", "")).lower()
        ]
        assert aborted, (
            "abort 后历史里应保留被中断的 assistant 消息（MessageAbortedError）: "
            f"{[(m['info']['role'], m['info'].get('finish'), m['info'].get('error')) for m in messages]}"
        )
        assert session_id not in env.status(client)
    finally:
        collector.stop()


def test_p12_event_stream(env: ProbeEnv, client: httpx.Client) -> None:
    collector = EventCollector(env.base_url)
    collector.start()
    try:
        collector.wait_connected()
        session_id, marker = new_session(env, client, "p12")
        env.prompt_and_wait(client, session_id, text=marker)
        collector.wait_for_type("session.idle", session_id=session_id, timeout=30)

        scoped = [
            record.payload
            for record in collector.records()
            if (record.payload.get("properties") or {}).get("sessionID") == session_id
        ]
        types = {str(event.get("type")) for event in scoped}
        assert {
            "message.part.delta",
            "message.part.updated",
            "message.updated",
        } <= types
        assert "session.idle" in {
            str(record.payload.get("type")) for record in collector.records()
        }
        deltas = [
            str((event.get("properties") or {}).get("delta", ""))
            for event in scoped
            if event.get("type") == "message.part.delta"
        ]
        assert deltas, "应收到 message.part.delta"
        assert all(delta for delta in deltas)

        messages = env.messages(client, session_id)
        assistant = [m for m in messages if m["info"]["role"] == "assistant"]
        assert assistant and assistant[-1]["info"]["finish"] == "stop"
    finally:
        collector.stop()


def test_p13_event_scope_and_delta(env: ProbeEnv, client: httpx.Client) -> None:
    dir_a = env.work_dir / "event-a"
    dir_b = env.work_dir / "event-b"
    dir_a.mkdir(parents=True, exist_ok=True)
    dir_b.mkdir(parents=True, exist_ok=True)
    default_collector = EventCollector(env.base_url)
    collector_a = EventCollector(env.base_url, directory=dir_a)
    collector_b = EventCollector(env.base_url, directory=dir_b)
    for collector in (default_collector, collector_a, collector_b):
        collector.start()
    try:
        for collector in (default_collector, collector_a, collector_b):
            collector.wait_connected()

        session_default, marker_default = new_session(env, client, "p13-default")
        session_a, marker_a = new_session(env, client, "p13-a", directory=dir_a)
        session_b, marker_b = new_session(env, client, "p13-b", directory=dir_b)

        env.prompt(
            client, session_default, text=f"SLOW {marker_default}"
        ).raise_for_status()
        env.prompt(client, session_a, text=marker_a, directory=dir_a).raise_for_status()
        env.prompt(client, session_b, text=marker_b, directory=dir_b).raise_for_status()
        for collector, session_id in (
            (default_collector, session_default),
            (collector_a, session_a),
            (collector_b, session_b),
        ):
            collector.wait_for_type("session.idle", session_id=session_id, timeout=40)

        def sessions_in(collector: EventCollector) -> set[str]:
            return {
                str((record.payload.get("properties") or {}).get("sessionID"))
                for record in collector.records()
                if (record.payload.get("properties") or {}).get("sessionID")
            }

        assert session_default in sessions_in(default_collector)
        assert session_a not in sessions_in(default_collector)
        assert session_b not in sessions_in(default_collector)
        assert session_a in sessions_in(collector_a)
        assert session_b not in sessions_in(collector_a)
        assert session_b in sessions_in(collector_b)
        assert session_a not in sessions_in(collector_b)

        deltas = [
            str((record.payload.get("properties") or {}).get("delta", ""))
            for record in default_collector.records()
            if record.payload.get("type") == "message.part.delta"
            and (record.payload.get("properties") or {}).get("sessionID")
            == session_default
        ]
        assert len(deltas) >= 2, f"delta 应逐块到达，实际={deltas}"
        assert "".join(deltas) == "PROBE_OK"
    finally:
        for collector in (default_collector, collector_a, collector_b):
            collector.stop()


def test_p14_restart_boundary(mock: MockProvider, standalone_env: Any) -> None:
    env = standalone_env()
    client = env.client()
    try:
        session_id, marker_1 = new_session(env, client, "p14-1")
        env.prompt_and_wait(
            client,
            session_id,
            text=marker_1,
            model=("mock", "mock-model-b"),
            tools={"probe_echo_upper": False},
        )
        env.prompt_and_wait(client, session_id, text=new_marker("p14-2"))
        before = env.messages(client, session_id)
        assert len(before) == 4

        env.restart()
        assert env.health_version() == "1.17.13"
        client.close()
        client = env.client()
        after = env.messages(client, session_id)
        assert message_roles(after) == message_roles(before)
        assert [m["info"]["id"] for m in after] == [m["info"]["id"] for m in before]

        marker_3 = new_marker("p14-3")
        env.prompt_and_wait(client, session_id, text=marker_3)
        body = mock.request_for(marker_3)
        assert body["model"] == "mock-model-b"
        assert "probe_echo_upper" not in set(mock.tool_names_for(marker_3))
        assert len(env.messages(client, session_id)) == 6
    finally:
        client.close()


def test_p15_restart_id_stability(mock: MockProvider, standalone_env: Any) -> None:
    env = standalone_env()
    client = env.client()
    try:
        session_id, _ = new_session(env, client, "p15")
        env.prompt_and_wait(client, session_id, text=new_marker("p15-1"))
        env.prompt_and_wait(client, session_id, text=new_marker("p15-2"))
        before = env.messages(client, session_id)
        before_ids = [
            (
                message["info"]["id"],
                tuple(part.get("id") for part in message.get("parts") or []),
            )
            for message in before
        ]

        env.restart()
        client.close()
        client = env.client()
        after = env.messages(client, session_id)
        after_ids = [
            (
                message["info"]["id"],
                tuple(part.get("id") for part in message.get("parts") or []),
            )
            for message in after
        ]
        assert after_ids == before_ids, (
            "重启后 message/part ID 应保持一致；若 OpenCode 重新生成，"
            f"before={before_ids}, after={after_ids}"
        )
        assert len({message_id for message_id, _ in after_ids}) == len(after_ids)

        marker_3 = new_marker("p15-3")
        env.prompt_and_wait(client, session_id, text=marker_3)
        messages = env.messages(client, session_id)
        assert len(messages) == 6
        new_ids = {message["info"]["id"] for message in messages[4:]}
        assert new_ids.isdisjoint({message_id for message_id, _ in before_ids})
    finally:
        client.close()


# ------------------------------------------------- 边界 / 跨会话隔离 / 历史与分页
def test_p16_tools_boundaries(
    env: ProbeEnv,
    client: httpx.Client,
    mock: MockProvider,
    baseline_tools: set[str],
) -> None:
    session_id, _ = new_session(env, client, "p16")
    upper = "probe_echo_upper"
    lower = "probe_echo_lower"
    slow = "probe_slow_echo"

    cases: list[tuple[dict[str, bool], set[str], str]] = [
        ({}, baseline_tools, "空对象"),
        ({"unknown_tool_xyz": False}, baseline_tools, "未知 key"),
        ({"echo_upper": False}, baseline_tools, "原始 MCP 名"),
        ({slow: False}, baseline_tools - {slow}, "禁用 slow"),
        (
            {upper: False, lower: False},
            baseline_tools - {upper, lower},
            "同时禁用 upper/lower（显式 map 覆盖上一轮，slow 恢复）",
        ),
        ({upper: True}, baseline_tools, "只恢复 upper"),
        ({lower: True}, baseline_tools, "再恢复 lower"),
        ({slow: True}, baseline_tools, "全部恢复"),
        ({"webfetch": False}, baseline_tools - {"webfetch"}, "禁用未暴露工具"),
        (
            {"webfetch": True, "skill": False},
            baseline_tools - {"skill"},
            "恢复 webfetch 并禁用 skill",
        ),
    ]
    for tools, expected, context in cases:
        marker = new_marker("p16")
        env.prompt_and_wait(client, session_id, text=marker, tools=tools)
        assert_tools(mock, marker, expected, context)


def test_p17_default_directory(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_id, _ = new_session(env, client, "p17")
    server_directory = env.server_directory(client)
    assert env.session(client, session_id)["directory"] == server_directory

    _, results = call_provider_tool(
        env, client, mock, session_id, "bash", tail="pwd", prefix="p17-pwd"
    )
    assert any(server_directory in result for result in results), (
        f"bash pwd 结果应包含 server 目录 {server_directory!r}: {results}"
    )


def test_p18_invalid_agent_and_model(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    collector = EventCollector(env.base_url)
    collector.start()
    try:
        collector.wait_connected()

        # 非法 agent：HTTP 204 admission，但 session.error 明确报错，且不落 user 消息。
        session_id, marker = new_session(env, client, "p18-agent")
        response = env.prompt(client, session_id, text=marker, agent="nope-agent")
        assert response.status_code == 204
        error = collector.wait_for_type(
            "session.error", session_id=session_id, timeout=20
        )
        assert "Agent not found" in event_error_text(error)
        time.sleep(0.3)
        assert not any(
            marker in message_text(message)
            for message in env.messages(client, session_id)
        )
        assert env.session(client, session_id).get("agent", "build") == "build"

        marker_ok = new_marker("p18-agent-ok")
        env.prompt_and_wait(client, session_id, text=marker_ok)
        system = system_text(mock.request_for(marker_ok))
        assert AGENT_A_PROMPT not in system
        assert AGENT_B_PROMPT not in system

        # 非法 model：明确报错，不调用 provider；随后可用显式合法 model 恢复。
        session_model, marker_model = new_session(env, client, "p18-model")
        response = env.prompt(
            client,
            session_model,
            text=marker_model,
            model=("mock", "nope-model"),
        )
        assert response.status_code == 204
        error = collector.wait_for_type(
            "session.error", session_id=session_model, timeout=20
        )
        assert "Model not found" in event_error_text(error)
        assert mock.count_for(marker_model) == 0
        # 已知风险：非法 model 会写进 session，后续省略 model 仍会失败。
        assert env.session(client, session_model)["model"]["id"] == "nope-model"

        marker_recover = new_marker("p18-recover")
        env.prompt_and_wait(
            client,
            session_model,
            text=marker_recover,
            model=("mock", "mock-model"),
        )
        assert mock.request_for(marker_recover)["model"] == "mock-model"
    finally:
        collector.stop()


def test_p19_provider_failure_is_bounded(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    collector = EventCollector(env.base_url)
    collector.start()
    errors: list[dict[str, Any]] = []
    try:
        collector.wait_connected()

        def observe(label: str, prefix: str) -> None:
            session_id, _ = new_session(env, client, prefix)
            marker = new_marker(prefix)
            response = env.prompt(client, session_id, text=f"{label} {marker}")
            assert response.status_code == 204
            mock.wait_for_marker(marker, count=1, timeout=30)
            deadline = time.time() + 20
            retry_status: dict[str, Any] | None = None
            while time.time() < deadline:
                status = env.status(client).get(session_id)
                if (
                    isinstance(status, dict)
                    and status.get("type") == "retry"
                    and int(status.get("attempt", 0)) >= 2
                ):
                    retry_status = status
                    break
                time.sleep(0.2)
            assert retry_status is not None, (
                f"{label}: 20s 内未观察到 attempt>=2 的 retry 状态; "
                f"status={env.status(client).get(session_id)}"
            )
            assert 1 <= mock.count_for(marker) <= 5, mock.count_for(marker)
            errors.extend(
                event
                for event in collector.snapshot()
                if event.get("type") == "session.error"
                and (event.get("properties") or {}).get("sessionID") == session_id
            )
            env.abort(client, session_id)
            deadline = time.time() + 10
            while time.time() < deadline:
                if not env.status(client).get(session_id):
                    break
                time.sleep(0.1)

        observe("ERROR500", "p19-500")
        observe("TIMEOUT", "p19-timeout")
    finally:
        collector.stop()

    if not errors:
        pytest.xfail(
            "1.17.13 对 provider 500/断连只进入无限 retry（attempt 递增、next 时间拉长），"
            "120s 内不产生 session.error，也不会自动终止；该场景记为 PARTIAL"
        )
    assert errors


def test_p20_legacy_message_id_bounded(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    # 正常路径：每轮恰好 1 次 provider 请求。
    session_id, marker = new_session(env, client, "p20-normal")
    env.prompt_and_wait(client, session_id, text=marker)
    assert mock.count_for(marker) == 1

    # 自定义 messageID：只要求有界，不要求精确次数。若 10s 内请求数超过 6，
    # watchdog 主动 abort 并失败，避免测试自己挂死。
    custom_id = "msg_1" + "0" * 25
    custom_marker = new_marker("p20-custom")
    response = env.prompt(client, session_id, text=custom_marker, message_id=custom_id)
    assert response.status_code in {200, 204}, response.text
    deadline = time.time() + 10.0
    max_requests = 0
    while time.time() < deadline:
        max_requests = max(max_requests, mock.count_for(custom_marker))
        if max_requests > 6:
            env.abort(client, session_id)
            pytest.fail(
                f"legacy messageID={custom_id} 触发失控，"
                f"10s 内已发 {max_requests} 次 provider 请求"
            )
        if max_requests and not env.status(client).get(session_id):
            break
        time.sleep(0.1)
    assert max_requests <= 6, f"provider 请求数应有界，实际={max_requests}"
    env.abort(client, session_id)


def test_p21_cross_session_isolation(
    env: ProbeEnv,
    client: httpx.Client,
    mock: MockProvider,
    baseline_tools: set[str],
) -> None:
    upper = "probe_echo_upper"
    lower = "probe_echo_lower"
    session_a, _ = new_session(env, client, "p21-a")
    session_b, _ = new_session(env, client, "p21-b")

    marker_a_off = new_marker("p21-a-off")
    env.prompt_and_wait(client, session_a, text=marker_a_off, tools={upper: False})
    assert_tools(mock, marker_a_off, baseline_tools - {upper}, "A 禁用")

    marker_b_full = new_marker("p21-b-full")
    env.prompt_and_wait(client, session_b, text=marker_b_full)
    assert_tools(mock, marker_b_full, baseline_tools, "B 不受 A 影响")

    marker_a_sticky = new_marker("p21-a-sticky")
    env.prompt_and_wait(client, session_a, text=marker_a_sticky)
    assert_tools(mock, marker_a_sticky, baseline_tools - {upper}, "A 仍禁用")

    marker_b_off = new_marker("p21-b-off")
    env.prompt_and_wait(client, session_b, text=marker_b_off, tools={lower: False})
    assert_tools(mock, marker_b_off, baseline_tools - {lower}, "B 禁用 lower")

    marker_a_on = new_marker("p21-a-on")
    env.prompt_and_wait(client, session_a, text=marker_a_on, tools={upper: True})
    assert_tools(mock, marker_a_on, baseline_tools, "A 恢复不影响 B")

    marker_b_sticky = new_marker("p21-b-sticky")
    env.prompt_and_wait(client, session_b, text=marker_b_sticky)
    assert_tools(mock, marker_b_sticky, baseline_tools - {lower}, "B 仍禁用 lower")


def test_p22_history_shape_and_pagination(env: ProbeEnv, client: httpx.Client) -> None:
    session_id, _ = new_session(env, client, "p22")
    markers = [new_marker(f"p22-{index}") for index in range(3)]
    for marker in markers:
        env.prompt_and_wait(client, session_id, text=marker)

    messages = env.messages(client, session_id)
    assert len(messages) == 6
    assert message_roles(messages) == ["user", "assistant"] * 3
    ids = [message["info"]["id"] for message in messages]
    assert len(set(ids)) == len(ids)
    assert [message_text(m) for m in messages[::2]] == markers

    for message in messages:
        info = message["info"]
        assert info["sessionID"] == session_id
        assert info["id"].startswith("msg_")
        parts = message.get("parts") or []
        assert parts
        for part in parts:
            assert part["id"].startswith("prt_")
            assert part["sessionID"] == session_id
            assert part["messageID"] == info["id"]
        if info["role"] == "user":
            assert {part["type"] for part in parts} == {"text"}
        else:
            assert "text" in {part["type"] for part in parts}
            assert message_text(message) == "PROBE_OK"

    # limit 原生生效：返回最近 N 条，且仍按时间正序。
    limited = env.messages(client, session_id, params={"limit": 2})
    assert limited == messages[-2:]
    assert len(env.messages(client, session_id, params={"limit": 1})) == 1
    assert env.messages(client, session_id, params={"limit": 100}) == messages
    assert env.messages(client, session_id, params={"limit": 0}) == messages

    # before：1.17.13 对合法 message ID 返回 400，分页不可用；若后续版本支持
    # 则校验确实向前翻页。
    before = client.get(
        f"/session/{session_id}/message",
        params={"before": messages[0]["info"]["id"], "limit": 1},
    )
    if before.status_code == 200:
        assert len(before.json()) <= 1
    else:
        assert before.status_code == 400, before.text
    bogus = client.get(
        f"/session/{session_id}/message", params={"before": "bogus", "limit": 1}
    )
    assert bogus.status_code == 400

    unknown = client.get("/session/ses_does_not_exist/message")
    assert unknown.status_code in {400, 404}
    # 非 session ID 形状（缺少 ses_ 前缀）会触发 500，已记录为 1.17.13 缺陷。
    illegal = client.get("/session/not-a-session/message")
    assert illegal.status_code in {400, 404, 500}
