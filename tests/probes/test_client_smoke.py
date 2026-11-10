"""使用生产 ``OpenCodeClient`` 对真实 server 的冒烟测试。

必跑项覆盖单轮流式、同会话多轮、运行中 abort、流中杀 server；其余用例标
``optional``，不属于默认必跑集合。失败时只记录结果，不在探针里修改生产代码。
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Any

import pytest

from tests.probes.conftest import new_marker
from tests.probes.mock_provider import MockProvider
from tests.probes.probe_env import ProbeEnv
from witty_agent_server.infra.clients.opencode_client import (
    OpenCodeClient,
    OpenCodeClientError,
)

pytestmark = [pytest.mark.probe, pytest.mark.client_smoke]

STREAM_TIMEOUT = 60.0


def make_client(env: ProbeEnv) -> OpenCodeClient:
    return OpenCodeClient(serve_port=env.port, timeout=30.0)


def run_stream(
    client: OpenCodeClient,
    session_key: str,
    message: str,
    *,
    timeout: float = STREAM_TIMEOUT,
) -> tuple[dict[str, Any], bool]:
    """在线程里消费 ``stream_turn``，返回 (结果, 是否正常退出)。

    线程仍存活时主动 abort，并再等 5s；调用方必须把 ``False`` 当作失败，
    避免测试自己跟着 generator 一起挂死。
    """
    result: dict[str, Any] = {"events": [], "error": None}

    def consume() -> None:
        try:
            for event in client.stream_turn(session_key=session_key, message=message):
                result["events"].append(event)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=consume, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        with contextlib.suppress(Exception):
            client.abort_session(session_key=session_key)
        thread.join(5)
        return result, False
    return result, True


def wait_busy(env: ProbeEnv, session_id: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    with env.client() as raw:
        while time.time() < deadline:
            if env.status(raw).get(session_id):
                return
            time.sleep(0.05)
    raise TimeoutError(f"session {session_id} never became busy")


def terminal_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event
        for event in events
        if event.get("type") == "session.idle"
        or (
            event.get("type") == "message.updated"
            and (event.get("info") or {}).get("finish") == "stop"
        )
        or event.get("type") == "session.error"
    ]


def cleanup_session(client: OpenCodeClient, session_key: str) -> None:
    with contextlib.suppress(Exception):
        client.delete_session(session_key=session_key)


# ------------------------------------------------------------ 必跑：创建 / 多轮 / abort
def test_c1_create_and_stream_one_turn(env: ProbeEnv, mock: MockProvider) -> None:
    client = make_client(env)
    key = new_marker("c1")
    session_id = client.create_session(session_key=key)
    try:
        marker = new_marker("c1-msg")
        result, finished = run_stream(client, key, marker)
        assert finished, f"stream_turn 挂起，已收到事件={result['events']}"
        assert result["error"] is None, result["error"]
        assert mock.count_for(marker) >= 1
        events = result["events"]
        assert any(
            event.get("type") == "message.part.delta" and event.get("delta")
            for event in events
        ), events
        assert terminal_events(events), events
        assert client.resolve_session_id(key) == session_id
    finally:
        cleanup_session(client, key)
        client.close()


def test_c2_two_turns_same_session(env: ProbeEnv, mock: MockProvider) -> None:
    client = make_client(env)
    key = new_marker("c2")
    client.create_session(session_key=key)
    try:
        for index in (1, 2):
            marker = new_marker(f"c2-{index}")
            result, finished = run_stream(client, key, marker)
            assert finished, f"第 {index} 轮挂起: {result['events']}"
            assert result["error"] is None, result["error"]
            assert terminal_events(result["events"]), result["events"]
            assert any(
                event.get("type") == "message.part.delta" and event.get("delta")
                for event in result["events"]
            ), result["events"]
            assert mock.count_for(marker) >= 1
    finally:
        cleanup_session(client, key)
        client.close()


def test_c3_abort_while_running(env: ProbeEnv, mock: MockProvider) -> None:
    client = make_client(env)
    key = new_marker("c3")
    session_id = client.create_session(session_key=key)
    marker = new_marker("c3-msg")
    result: dict[str, Any] = {"events": [], "error": None}

    def consume() -> None:
        try:
            for event in client.stream_turn(session_key=key, message=f"SLOW {marker}"):
                result["events"].append(event)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=consume, daemon=True)
    try:
        thread.start()
        mock.wait_for_marker(marker, count=1, timeout=30)
        wait_busy(env, session_id)
        client.abort_session(session_key=key)
        thread.join(30)
        assert not thread.is_alive(), f"abort 后 stream_turn 未退出: {result['events']}"
        assert result["error"] is None, result["error"]
        types = {event.get("type") for event in result["events"]}
        assert "session.error" in types or terminal_events(result["events"]), types
    finally:
        with contextlib.suppress(Exception):
            client.abort_session(session_key=key)
        cleanup_session(client, key)
        client.close()


# ------------------------------------------------------------ 必跑：流中杀 server
def test_c8_server_killed_during_stream(
    mock: MockProvider, standalone_env: Any
) -> None:
    env = standalone_env()
    client = make_client(env)
    key = new_marker("c8")
    client.create_session(session_key=key)
    marker = new_marker("c8-msg")
    result: dict[str, Any] = {"events": [], "error": None}

    def consume() -> None:
        try:
            for event in client.stream_turn(session_key=key, message=f"SLOW {marker}"):
                result["events"].append(event)
        except BaseException as exc:
            result["error"] = exc

    thread = threading.Thread(target=consume, daemon=True)
    try:
        thread.start()
        mock.wait_for_marker(marker, count=1, timeout=30)
        env.stop(cleanup=False)
        thread.join(20)
        assert not thread.is_alive(), (
            f"server killed 后 client 仍在等待: {result['events']}"
        )
        assert result["error"] is not None, (
            "断 SSE / 杀 server 时 stream_turn 应抛错，不能静默返回: "
            f"{result['events']}"
        )
    finally:
        with contextlib.suppress(Exception):
            client.close()


# ------------------------------------------------------------ 可选诊断
@pytest.mark.optional
def test_c4_idle_abort_and_unknown_session(
    env: ProbeEnv,
) -> None:
    client = make_client(env)
    key = new_marker("c4")
    client.create_session(session_key=key)
    try:
        client.abort_session(session_key=key)  # 空闲 abort 是 no-op
    finally:
        cleanup_session(client, key)
        client.close()

    client = make_client(env)
    try:
        with pytest.raises(OpenCodeClientError) as excinfo:
            client.abort_session(session_key=new_marker("c4-missing"))
        assert excinfo.value.status == 409, excinfo.value
    finally:
        client.close()


@pytest.mark.optional
def test_c5_delete_then_stream(env: ProbeEnv) -> None:
    client = make_client(env)
    key = new_marker("c5")
    client.create_session(session_key=key)
    client.delete_session(session_key=key)
    try:
        with pytest.raises(OpenCodeClientError) as excinfo:
            list(client.stream_turn(session_key=key, message="after-delete"))
        assert excinfo.value.status == 409, excinfo.value
    finally:
        client.close()


@pytest.mark.optional
def test_c6_duplicate_title_ambiguous(
    env: ProbeEnv,
) -> None:
    key = new_marker("c6")
    with env.client() as raw:
        first = env.create_session(raw, title=key)
        second = env.create_session(raw, title=key)
    client = make_client(env)
    try:
        with pytest.raises(OpenCodeClientError) as excinfo:
            client.find_session_id(session_key=key)
        assert excinfo.value.status == 409, excinfo.value
        assert "ambiguous" in excinfo.value.message
    finally:
        with env.client() as raw:
            raw.delete(f"/session/{first}")
            raw.delete(f"/session/{second}")
        client.close()


@pytest.mark.optional
def test_c7_restart_then_continue(mock: MockProvider, standalone_env: Any) -> None:
    env = standalone_env()
    client = make_client(env)
    key = new_marker("c7")
    session_id = client.create_session(session_key=key)
    try:
        first_marker = new_marker("c7-1")
        result, finished = run_stream(client, key, first_marker)
        assert finished and result["error"] is None, result["error"]
        assert mock.count_for(first_marker) >= 1

        env.restart()
        client.update_config(serve_port=env.port)
        second_marker = new_marker("c7-2")
        result, finished = run_stream(client, key, second_marker)
        assert finished, f"restart 后 stream_turn 挂起: {result['events']}"
        assert result["error"] is None, result["error"]
        assert terminal_events(result["events"]), result["events"]

        fresh_client = make_client(env)
        try:
            assert fresh_client.find_session_id(session_key=key) == session_id
        finally:
            fresh_client.close()

        with env.client() as raw:
            messages = env.messages(raw, session_id)
        assert [m["info"]["role"] for m in messages] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
    finally:
        cleanup_session(client, key)
        client.close()
