"""OpenCode 1.17.13 进程内 ``/api``（v2）能力探针。

覆盖 v2 的会话创建与 admission、agent/model 会话级切换、事件流作用域、
interrupt、revert 三态、历史/消息分页，以及 v2 与 legacy 面的投影隔离。

与 ``test_opencode_1_17_13.py``（legacy 面）并列：两条面的消息和历史投影互不
可见，本套件不读写 legacy 会话。

运行：

    WITTY_PROBE_OPENCODE_DIR=/opt/witty-service/opencode-1.17.13 \
      .venv/bin/python -m pytest tests/probes/test_opencode_1_17_13_api.py \
      -m probe -q

断言的是 pinned 版本的真实行为；与 legacy 面的差异记录在
`docs/probes/opencode-1-17-13-api.md`，不在这里做兼容层。
"""

from __future__ import annotations

import time
import uuid
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

MODEL = ("mock", "mock-model")
MODEL_B = ("mock", "mock-model-b")
MCP_TOOL_NAMES = {"probe_echo_upper", "probe_echo_lower", "probe_slow_echo"}
RAW_MCP_NAMES = {"echo_upper", "echo_lower", "slow.echo", "slow_echo"}
TERMINAL = {
    "session.next.step.ended",
    "session.next.step.failed",
    "session.error",
    "session.idle",
}
NORMAL_TURN_TYPES = [
    "session.next.prompt.admitted",
    "session.next.prompted",
    "session.next.step.started",
    "session.next.text.started",
    "session.next.text.ended",
    "session.next.step.ended",
]


# --------------------------------------------------------------------------- helpers
def v2_create(
    client: httpx.Client,
    env: ProbeEnv,
    *,
    directory: Any = None,
    model: tuple[str, str] | None = MODEL,
    agent: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "location": {"directory": str(directory or env.work_dir)},
        **extra,
    }
    if model is not None:
        body["model"] = {"providerID": model[0], "id": model[1]}
    if agent is not None:
        body["agent"] = agent
    response = client.post("/api/session", json=body)
    assert response.status_code == 200, response.text
    return dict(response.json()["data"])


def v2_prompt(
    client: httpx.Client,
    session_id: str,
    text: str,
    *,
    message_id: str | None = None,
    resume: bool | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {"prompt": {"text": text}}
    if message_id is not None:
        body["id"] = message_id
    if resume is not None:
        body["resume"] = resume
    return client.post(f"/api/session/{session_id}/prompt", json=body)


def v2_history_body(
    client: httpx.Client, session_id: str, **params: Any
) -> dict[str, Any]:
    params.setdefault("limit", 100)
    response = client.get(f"/api/session/{session_id}/history", params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


def v2_events(
    client: httpx.Client, session_id: str, **params: Any
) -> list[dict[str, Any]]:
    return list(v2_history_body(client, session_id, **params)["data"])


def v2_message_body(
    client: httpx.Client, session_id: str, **params: Any
) -> dict[str, Any]:
    response = client.get(f"/api/session/{session_id}/message", params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


def v2_messages(
    client: httpx.Client, session_id: str, **params: Any
) -> list[dict[str, Any]]:
    return list(v2_message_body(client, session_id, **params)["data"])


def v2_session(client: httpx.Client, session_id: str) -> dict[str, Any]:
    response = client.get(f"/api/session/{session_id}")
    assert response.status_code == 200, response.text
    return dict(response.json()["data"])


def v2_last_seq(client: httpx.Client, session_id: str) -> int:
    seqs = [int(event["durable"]["seq"]) for event in v2_events(client, session_id)]
    return max(seqs or [0])


def v2_wait_turn(
    client: httpx.Client, session_id: str, after_seq: int, timeout: float = 40.0
) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for event in v2_events(client, session_id):
            if event["durable"]["seq"] > after_seq and event["type"] in TERMINAL:
                return event
        time.sleep(0.05)
    types = [event["type"] for event in v2_events(client, session_id)]
    raise TimeoutError(
        f"v2 session {session_id} 在 {timeout}s 内没有新的终态；最近事件={types[-12:]}"
    )


def v2_turn(
    client: httpx.Client, session_id: str, text: str, timeout: float = 40.0
) -> dict[str, Any]:
    before = v2_last_seq(client, session_id)
    response = v2_prompt(client, session_id, text)
    assert response.status_code == 200, response.text
    return v2_wait_turn(client, session_id, before, timeout)


def v2_active(client: httpx.Client) -> dict[str, Any]:
    response = client.get("/api/session/active")
    assert response.status_code == 200, response.text
    return dict(response.json()["data"])


def v2_wait_active(
    client: httpx.Client, session_id: str, timeout: float = 20.0
) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if session_id in v2_active(client):
            return
        time.sleep(0.05)
    raise TimeoutError(f"session {session_id} 未进入 running")


def v2_wait_idle(client: httpx.Client, session_id: str, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if session_id not in v2_active(client):
            return
        time.sleep(0.05)
    raise TimeoutError(f"session {session_id} 未回到 idle")


def event_body(payload: dict[str, Any]) -> dict[str, Any]:
    """durable/native 事件用 ``data``，legacy 帧用 ``properties``。"""
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    properties = payload.get("properties")
    return properties if isinstance(properties, dict) else {}


def records_for(collector: EventCollector, session_id: str) -> list[Any]:
    return [
        record
        for record in collector.records()
        if event_body(record.payload).get("sessionID") == session_id
    ]


def event_types(records: list[Any]) -> list[str]:
    return [str(record.payload.get("type") or "") for record in records]


def joined_deltas(records: list[Any]) -> str:
    return "".join(
        str(event_body(record.payload).get("delta") or "")
        for record in records
        if record.payload.get("type") == "session.next.text.delta"
    )


def assistant_text(message: dict[str, Any]) -> str:
    return "\n".join(
        str(part.get("text", ""))
        for part in message.get("content") or []
        if part.get("type") == "text"
    )


def user_texts(body: dict[str, Any]) -> list[str]:
    return [
        str(message.get("content"))
        for message in body.get("messages") or []
        if message.get("role") == "user" and isinstance(message.get("content"), str)
    ]


def warm_location(
    client: httpx.Client,
    env: ProbeEnv,
    mock: MockProvider,
    directory: Any,
    *,
    timeout: float = 40.0,
) -> str:
    """把一个 location 的异步服务与 model catalog 预热到可执行状态。

    v2 的 catalog 按 location 异步就绪；在未就绪时 prompt 仍返回 200，但回合
    会静默停在 ``prompted``，没有 step/error。这里用独立 session 反复投递
    直到出现 step.started，真实用例随后再建干净 session。
    """
    session = v2_create(client, env, directory=directory)
    session_id = str(session["id"])
    deadline = time.time() + timeout
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        before = v2_last_seq(client, session_id)
        marker = new_marker(f"warm-{attempts}")
        response = v2_prompt(client, session_id, f"PROBE {marker}")
        assert response.status_code == 200, response.text
        try:
            v2_wait_turn(client, session_id, before, timeout=5)
            return session_id
        except TimeoutError:
            time.sleep(0.4)
    raise AssertionError(
        f"location {directory} 在 {timeout}s 内没有进入可执行状态；"
        f"provider 请求数={len(mock.snapshot())}"
    )


@pytest.fixture(scope="module", autouse=True)
def work_dir_ready(env: ProbeEnv, client: httpx.Client, mock: MockProvider) -> None:
    warm_location(client, env, mock, env.work_dir)


def v2_switch_agent(client: httpx.Client, session_id: str, agent: str) -> None:
    response = client.post(f"/api/session/{session_id}/agent", json={"agent": agent})
    assert response.status_code == 204, response.text


def v2_switch_model(
    client: httpx.Client, session_id: str, model: tuple[str, str]
) -> None:
    response = client.post(
        f"/api/session/{session_id}/model",
        json={"model": {"providerID": model[0], "id": model[1]}},
    )
    assert response.status_code == 204, response.text


def v2_location(client: httpx.Client, directory: Any = None) -> dict[str, Any]:
    params = {"location[directory]": str(directory)} if directory is not None else None
    response = client.get("/api/location", params=params)
    assert response.status_code == 200, response.text
    return dict(response.json())


def v2_agents(client: httpx.Client, directory: Any) -> list[dict[str, Any]]:
    response = client.get("/api/agent", params={"location[directory]": str(directory)})
    assert response.status_code == 200, response.text
    return list(response.json()["data"])


def system_text(body: dict[str, Any]) -> str:
    return "\n".join(
        str(message.get("content", ""))
        for message in body.get("messages") or []
        if message.get("role") == "system"
    )


def part_ids(message: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(part.get("id", ""))
        for part in message.get("content") or []
        if isinstance(part, dict)
    )


@pytest.fixture(scope="module")
def v2_baseline_tools(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> set[str]:
    """v2 会话默认暴露给 provider 的工具集合（与 legacy baseline 不同）。"""
    session_id = str(v2_create(client, env)["id"])
    marker = new_marker("v2-baseline")
    v2_turn(client, session_id, marker)
    return set(mock.tool_names_for(marker))


# -------------------------------------------------------------------- 会话与 surface
def test_create_admission_and_turn_projection(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    requested_title = new_marker("title")
    session = v2_create(client, env, title=requested_title)
    session_id = str(session["id"])
    assert session_id.startswith("ses_")
    assert session["location"]["directory"] == str(env.work_dir)
    assert session["model"] == {
        "id": "mock-model",
        "providerID": "mock",
        "variant": "default",
    }
    # v2 create 没有 title 入参，传了也被忽略，标题自动生成。
    assert session["title"].startswith("New session - ")
    assert requested_title not in session["title"]

    marker = new_marker("e1")
    message_id = f"msg_{uuid.uuid4().hex}"
    response = v2_prompt(client, session_id, f"PROBE {marker}", message_id=message_id)
    assert response.status_code == 200, response.text
    admitted = dict(response.json()["data"])
    assert set(admitted) == {
        "id",
        "admittedSeq",
        "sessionID",
        "prompt",
        "delivery",
        "timeCreated",
    }
    assert admitted["id"] == message_id
    assert admitted["sessionID"] == session_id
    assert admitted["admittedSeq"] == 1
    assert admitted["prompt"]["text"] == f"PROBE {marker}"
    assert admitted["delivery"] == "steer"

    terminal = v2_wait_turn(client, session_id, 0)
    assert terminal["type"] == "session.next.step.ended"
    assert terminal["data"]["finish"] == "stop"

    events = v2_events(client, session_id)
    assert [event["type"] for event in events] == NORMAL_TURN_TYPES
    assert [event["durable"]["seq"] for event in events] == [1, 2, 3, 4, 5, 6]
    for event in events:
        assert event["id"].startswith("evt_")
        assert event["durable"]["aggregateID"] == session_id
        assert event["durable"]["version"] >= 1
        assert event["data"]["sessionID"] == session_id
    text_ended = events[4]["data"]
    assert text_ended["text"] == "PROBE_OK"
    assert text_ended["assistantMessageID"] == terminal["data"]["assistantMessageID"]

    messages = v2_messages(client, session_id)
    assert [message["type"] for message in messages] == ["assistant", "user"]
    assistant, user = messages
    assert user["id"] == message_id
    assert user["text"] == f"PROBE {marker}"
    assert assistant["agent"] == "build"
    assert assistant["model"]["id"] == "mock-model"
    assert assistant["finish"] == "stop"
    assert assistant_text(assistant) == "PROBE_OK"
    assert mock.request_for(marker)["model"] == "mock-model"

    # 同一个 id + 同一 text 再投一次：幂等 200，不重复执行。
    duplicate = v2_prompt(client, session_id, f"PROBE {marker}", message_id=message_id)
    assert duplicate.status_code == 200
    replayed = duplicate.json()["data"]
    assert replayed["id"] == message_id
    assert replayed["admittedSeq"] == admitted["admittedSeq"]
    assert mock.count_for(marker) == 1
    assert len(v2_events(client, session_id)) == len(events)
    # 同一 id + 不同 text 才是 409。
    conflict = v2_prompt(
        client, session_id, "PROBE different text", message_id=message_id
    )
    assert conflict.status_code == 409
    assert conflict.json()["_tag"] == "ConflictError"

    # 空闲 interrupt 仍返回 204；wait 端点未实现（503）。
    assert client.post(f"/api/session/{session_id}/interrupt").status_code == 204
    wait = client.post(f"/api/session/{session_id}/wait")
    assert wait.status_code == 503
    assert wait.json()["_tag"] == "ServiceUnavailableError"


def test_agent_and_model_switch(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session = v2_create(client, env)
    session_id = str(session["id"])

    assert (
        client.post(
            f"/api/session/{session_id}/agent", json={"agent": "probe-a"}
        ).status_code
        == 204
    )
    assert (
        client.post(
            f"/api/session/{session_id}/model",
            json={"model": {"providerID": MODEL_B[0], "id": MODEL_B[1]}},
        ).status_code
        == 204
    )

    events = v2_events(client, session_id)
    assert [event["type"] for event in events] == [
        "session.next.agent.switched",
        "session.next.model.switched",
    ]
    assert events[0]["data"]["agent"] == "probe-a"
    assert events[1]["data"]["model"] == {"id": "mock-model-b", "providerID": "mock"}

    messages = v2_messages(client, session_id)
    assert [message["type"] for message in messages] == [
        "model-switched",
        "agent-switched",
    ]
    assert messages[1]["agent"] == "probe-a"

    marker = new_marker("switch")
    v2_turn(client, session_id, marker)
    request = mock.request_for(marker)
    assert request["model"] == "mock-model-b"
    assert "PROBE_AGENT_A_SYSTEM_PROMPT" in str(request.get("messages"))
    assistant = v2_messages(client, session_id)[0]
    assert assistant["agent"] == "probe-a"
    assert assistant["model"]["id"] == "mock-model-b"


def test_cross_face_projection_isolation(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    marker = new_marker("v2face")
    session = v2_create(client, env)
    session_id = str(session["id"])
    v2_turn(client, session_id, f"PROBE {marker}")

    legacy_messages = client.get(f"/session/{session_id}/message")
    assert legacy_messages.status_code == 200
    assert legacy_messages.json() == []
    legacy_session = client.get(f"/session/{session_id}")
    assert legacy_session.status_code == 200
    assert legacy_session.json()["id"] == session_id

    legacy_marker = new_marker("legacyface")
    legacy_id = env.create_session(client, title=legacy_marker)
    env.prompt_and_wait(client, legacy_id, text=f"PROBE {legacy_marker}")
    assert v2_events(client, legacy_id) == []
    assert v2_messages(client, legacy_id) == []
    assert v2_session(client, legacy_id)["id"] == legacy_id
    assert legacy_id != session_id
    assert mock.count_for(legacy_marker) == 1


def test_error_status_matrix(env: ProbeEnv, client: httpx.Client) -> None:
    session_id = str(v2_create(client, env)["id"])

    invalid = client.get("/api/session/not-a-session")
    assert invalid.status_code == 400
    assert invalid.json()["_tag"] == "InvalidRequestError"

    for path in (
        "/api/session/ses_nope",
        "/api/session/ses_nope/history",
        "/api/session/ses_nope/message",
        "/api/session/ses_nope/context",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json()["_tag"] == "SessionNotFoundError"
    for path, body in (
        ("/api/session/ses_nope/interrupt", None),
        ("/api/session/ses_nope/revert/stage", {"messageID": "msg_x"}),
        ("/api/session/ses_nope/revert/commit", None),
        ("/api/session/ses_nope/revert/clear", None),
        ("/api/session/ses_nope/agent", {"agent": "probe-a"}),
        (
            "/api/session/ses_nope/model",
            {"model": {"providerID": "mock", "id": "mock-model"}},
        ),
    ):
        response = client.post(path, json=body)
        assert response.status_code == 404, path
        assert response.json()["_tag"] == "SessionNotFoundError"

    for params, tag in (
        ({"limit": 101}, "InvalidRequestError"),
        ({"limit": 0}, "InvalidRequestError"),
        ({"after": "bogus"}, "InvalidRequestError"),
    ):
        response = client.get(f"/api/session/{session_id}/history", params=params)
        assert response.status_code == 400, params
        assert response.json()["_tag"] == tag
    for params, tag in (
        ({"limit": 0}, "InvalidRequestError"),
        ({"cursor": "bogus"}, "InvalidCursorError"),
    ):
        response = client.get(f"/api/session/{session_id}/message", params=params)
        assert response.status_code == 400, params
        assert response.json()["_tag"] == tag

    for body, field in (
        ({}, "prompt"),
        ({"prompt": {}}, "text"),
        ({"prompt": {"text": "x"}, "id": "bad"}, "msg_"),
        ({"prompt": {"text": "x"}, "resume": "yes"}, "resume"),
    ):
        response = client.post(f"/api/session/{session_id}/prompt", json=body)
        assert response.status_code == 400, body
        assert field in response.json()["message"]

    for path in ("commit", "clear"):
        assert (
            client.post(f"/api/session/{session_id}/revert/{path}").status_code == 204
        )

    missing = client.post(f"/api/session/{session_id}/revert/stage", json={})
    assert missing.status_code == 400
    assert missing.json()["_tag"] == "InvalidRequestError"
    bogus = client.post(
        f"/api/session/{session_id}/revert/stage", json={"messageID": "msg_bogus"}
    )
    assert bogus.status_code == 404
    assert bogus.json()["_tag"] == "MessageNotFoundError"


# ----------------------------------------------------------------------- 事件作用域
def test_event_scope_and_delta(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    dir_a = env.work_dir / "scope-a"
    dir_b = env.work_dir / "scope-b"
    dir_a.mkdir(exist_ok=True)
    dir_b.mkdir(exist_ok=True)
    warm_location(client, env, mock, dir_a)
    warm_location(client, env, mock, dir_b)

    session_id = str(v2_create(client, env, directory=dir_a)["id"])
    durable = EventCollector(env.base_url, path=f"/api/session/{session_id}/event")
    native = EventCollector(env.base_url, path="/api/event")
    legacy_a = EventCollector(env.base_url, directory=dir_a)
    legacy_b = EventCollector(env.base_url, directory=dir_b)
    legacy_none = EventCollector(env.base_url)
    for collector in (native, legacy_a, legacy_b, legacy_none):
        collector.start()
        collector.wait_connected(timeout=20)
    durable.start()
    time.sleep(0.5)
    try:
        marker = new_marker("e4")
        v2_turn(client, session_id, f"STREAM_ABC {marker}")
        time.sleep(0.6)

        durable_records = records_for(durable, session_id)
        durable_types = event_types(durable_records)
        assert "session.next.text.delta" not in durable_types
        assert "session.next.step.ended" in durable_types
        assert joined_deltas(durable_records) == ""
        text_ended = next(
            record.payload["data"]
            for record in durable_records
            if record.payload["type"] == "session.next.text.ended"
        )
        assert text_ended["text"] == "ABC"

        native_records = records_for(native, session_id)
        assert event_types(native_records).count("session.next.text.delta") == 3
        assert joined_deltas(native_records) == "ABC"
        for record in native_records:
            payload = record.payload
            assert "data" in payload and "properties" not in payload
            assert payload["location"]["directory"] == str(dir_a)
        delta = next(
            record.payload
            for record in native_records
            if record.payload["type"] == "session.next.text.delta"
        )
        assert delta["data"]["delta"] in {"A", "B", "C"}
        assert delta["data"]["sessionID"] == session_id

        legacy_records = records_for(legacy_a, session_id)
        assert event_types(legacy_records).count("session.next.text.delta") == 3
        assert joined_deltas(legacy_records) == "ABC"
        for record in legacy_records:
            assert "properties" in record.payload
            assert "data" not in record.payload
            assert "durable" not in record.payload

        assert records_for(legacy_b, session_id) == []
        assert records_for(legacy_none, session_id) == []
    finally:
        durable.stop()
        native.stop()
        legacy_a.stop()
        legacy_b.stop()
        legacy_none.stop()

    # active 是全局的；legacy /session/status 完全看不到 v2 会话。
    slow_marker = new_marker("e4slow")
    assert v2_prompt(client, session_id, f"SLOW {slow_marker}").status_code == 200
    mock.wait_for_marker(slow_marker, 1, timeout=20)
    v2_wait_active(client, session_id)
    assert v2_active(client)[session_id]["type"] == "running"
    for params in ({}, {"directory": str(dir_a)}):
        status = client.get("/session/status", params=params)
        assert status.status_code == 200
        assert session_id not in status.text
    assert client.post(f"/api/session/{session_id}/interrupt").status_code == 204
    v2_wait_idle(client, session_id)


# --------------------------------------------------------------------------- interrupt
def test_interrupt_keeps_partial_assistant(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_id = str(v2_create(client, env)["id"])
    durable = EventCollector(env.base_url, path=f"/api/session/{session_id}/event")
    native = EventCollector(env.base_url, path="/api/event")
    native.start()
    native.wait_connected(timeout=20)
    durable.start()
    time.sleep(0.4)
    try:
        marker = new_marker("interrupt")
        assert v2_prompt(client, session_id, f"SLOW {marker}").status_code == 200
        mock.wait_for_marker(marker, 1, timeout=20)
        v2_wait_active(client, session_id)

        deadline = time.time() + 20
        while time.time() < deadline and not joined_deltas(
            records_for(native, session_id)
        ):
            time.sleep(0.05)
        assert joined_deltas(records_for(native, session_id)), (
            "中断前没有收到任何 delta"
        )

        assert client.post(f"/api/session/{session_id}/interrupt").status_code == 204
        v2_wait_idle(client, session_id)
        time.sleep(0.4)

        messages = v2_messages(client, session_id)
        assistant = next(
            message for message in messages if message["type"] == "assistant"
        )
        assert assistant["finish"] == "error"
        assert assistant["error"]["message"] == "Provider turn interrupted"
        text = assistant_text(assistant)
        assert text and "PROBE_OK".startswith(text)

        native_records = records_for(native, session_id)
        assert "session.next.step.failed" in event_types(native_records)
        assert joined_deltas(native_records) == text

        durable_records = records_for(durable, session_id)
        failed = next(
            record.payload["data"]
            for record in durable_records
            if record.payload["type"] == "session.next.step.failed"
        )
        assert failed["assistantMessageID"] == assistant["id"]
        assert event_types(durable_records).count("session.next.text.delta") == 0
        assert client.post(f"/api/session/{session_id}/interrupt").status_code == 204
    finally:
        durable.stop()
        native.stop()


# ------------------------------------------------------------------------------ revert
def test_revert_stage_does_not_trim_context(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_id = str(v2_create(client, env)["id"])
    first = new_marker("rev1")
    second = new_marker("rev2")
    v2_turn(client, session_id, first)
    v2_turn(client, session_id, second)
    users = [
        message
        for message in v2_messages(client, session_id)
        if message["type"] == "user"
    ]
    assert [user["text"] for user in users] == [second, first]
    target = users[0]["id"]
    before_messages = v2_messages(client, session_id)
    before_events = v2_events(client, session_id)

    stage = client.post(
        f"/api/session/{session_id}/revert/stage", json={"messageID": target}
    )
    assert stage.status_code == 200
    staged = stage.json()["data"]
    assert set(staged) == {"messageID", "diff", "files"}
    assert staged["messageID"] == target
    assert isinstance(staged["diff"], str)
    assert isinstance(staged["files"], list)
    assert v2_session(client, session_id)["revert"]["messageID"] == target
    assert v2_messages(client, session_id) == before_messages
    events = v2_events(client, session_id)
    assert [event["type"] for event in events] == [
        event["type"] for event in before_events
    ] + ["session.next.revert.staged"]

    # stage 本身不裁剪上下文：下一轮 provider 仍能看两轮历史。
    third = new_marker("rev3")
    v2_turn(client, session_id, third)
    assert user_texts(mock.request_for(third)) == [first, second, third]
    assert v2_session(client, session_id)["revert"]["messageID"] == target
    assert v2_messages(client, session_id)[0]["type"] == "assistant"

    assert client.post(f"/api/session/{session_id}/revert/commit").status_code == 204
    assert "revert" not in v2_session(client, session_id)
    assert v2_events(client, session_id)[-1]["type"] == "session.next.revert.committed"


def test_revert_commit_truncates_after_staged_user(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_id = str(v2_create(client, env)["id"])
    first = new_marker("cmt1")
    second = new_marker("cmt2")
    v2_turn(client, session_id, first)
    v2_turn(client, session_id, second)
    messages = v2_messages(client, session_id)
    users = [message for message in messages if message["type"] == "user"]
    assert [user["text"] for user in users] == [second, first]
    assert len(messages) == 4
    target = users[0]["id"]

    stage = client.post(
        f"/api/session/{session_id}/revert/stage", json={"messageID": target}
    )
    assert stage.status_code == 200
    assert client.post(f"/api/session/{session_id}/revert/commit").status_code == 204
    assert "revert" not in v2_session(client, session_id)

    committed = v2_messages(client, session_id)
    assert [message["type"] for message in committed] == [
        "user",
        "assistant",
        "user",
    ]
    assert committed[0]["id"] == target
    assert committed[2]["id"] == users[1]["id"]

    third = new_marker("cmt3")
    v2_turn(client, session_id, third)
    assert user_texts(mock.request_for(third)) == [first, second, third]
    assert v2_events(client, session_id)[-1]["type"] == "session.next.step.ended"


def test_revert_clear_keeps_projection(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_id = str(v2_create(client, env)["id"])
    first = new_marker("clr1")
    second = new_marker("clr2")
    v2_turn(client, session_id, first)
    v2_turn(client, session_id, second)
    messages = v2_messages(client, session_id)
    target = next(message["id"] for message in messages if message["type"] == "user")

    assert (
        client.post(
            f"/api/session/{session_id}/revert/stage", json={"messageID": target}
        ).status_code
        == 200
    )
    assert client.post(f"/api/session/{session_id}/revert/clear").status_code == 204
    assert "revert" not in v2_session(client, session_id)
    assert v2_messages(client, session_id) == messages
    assert v2_events(client, session_id)[-1]["type"] == "session.next.revert.cleared"

    # 忙时 stage/commit 仍可调用，不返回 409；随后 interrupt 收尾。
    slow_marker = new_marker("clrbusy")
    assert v2_prompt(client, session_id, f"SLOW {slow_marker}").status_code == 200
    mock.wait_for_marker(slow_marker, 1, timeout=20)
    assert (
        client.post(
            f"/api/session/{session_id}/revert/stage", json={"messageID": target}
        ).status_code
        == 200
    )
    assert client.post(f"/api/session/{session_id}/revert/commit").status_code == 204
    assert client.post(f"/api/session/{session_id}/interrupt").status_code == 204
    v2_wait_idle(client, session_id)


# -------------------------------------------------------------------------- 历史与分页
def test_p22_v2_history_and_message_pagination(
    env: ProbeEnv, client: httpx.Client
) -> None:
    session_id = str(v2_create(client, env)["id"])
    markers = [new_marker(f"page{i}") for i in range(3)]
    for marker in markers:
        v2_turn(client, session_id, marker)

    events = v2_events(client, session_id)
    assert [event["durable"]["seq"] for event in events] == list(
        range(1, len(events) + 1)
    )
    assert len(events) == 18
    for event in events:
        assert event["id"].startswith("evt_")
        assert event["durable"]["aggregateID"] == session_id
        assert event["data"]["sessionID"] == session_id

    first_page = v2_history_body(client, session_id, limit=5)
    assert [event["durable"]["seq"] for event in first_page["data"]] == [1, 2, 3, 4, 5]
    assert first_page["hasMore"] is True
    second_page = v2_history_body(client, session_id, after=5, limit=5)
    assert [event["durable"]["seq"] for event in second_page["data"]] == [
        6,
        7,
        8,
        9,
        10,
    ]
    last_page = v2_history_body(client, session_id, after=15, limit=5)
    assert [event["durable"]["seq"] for event in last_page["data"]] == [16, 17, 18]
    assert last_page["hasMore"] is False

    messages = v2_message_body(client, session_id)
    data = messages["data"]
    assert [message["type"] for message in data] == [
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert [message["text"] for message in data if message["type"] == "user"] == list(
        reversed(markers)
    )
    for message in data:
        if message["type"] == "assistant":
            assert assistant_text(message) == "PROBE_OK"
    assert set(messages["cursor"]) == {"previous", "next"}

    # message 的 limit 上界与 history 不同：101 仍可用（history 的 101 会 400）。
    assert len(v2_message_body(client, session_id, limit=101)["data"]) == len(data)

    newest_body = v2_message_body(client, session_id, limit=2)
    newest = list(newest_body["data"])
    assert [message["id"] for message in newest] == [data[0]["id"], data[1]["id"]]
    second = v2_message_body(
        client, session_id, limit=2, cursor=newest_body["cursor"]["next"]
    )
    assert [message["id"] for message in second["data"]] == [
        data[2]["id"],
        data[3]["id"],
    ]
    assert not (
        {message["id"] for message in newest}
        & {message["id"] for message in second["data"]}
    )
    back = v2_message_body(
        client, session_id, limit=2, cursor=second["cursor"]["previous"]
    )
    assert [message["id"] for message in back["data"]] == [
        data[0]["id"],
        data[1]["id"],
    ]


# --------------------------------------------------------------------- v2 独有语义
def test_v2_resume_false_only_admits_then_resume_true_executes(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_id = str(v2_create(client, env)["id"])
    marker = new_marker("resume")
    message_id = f"msg_{uuid.uuid4().hex}"

    admitted = v2_prompt(
        client, session_id, marker, message_id=message_id, resume=False
    )
    assert admitted.status_code == 200
    time.sleep(1.0)
    assert mock.count_for(marker) == 0
    assert [event["type"] for event in v2_events(client, session_id)] == [
        "session.next.prompt.admitted"
    ]

    resumed = v2_prompt(client, session_id, marker, message_id=message_id, resume=True)
    assert resumed.status_code == 200
    terminal = v2_wait_turn(client, session_id, 0)
    assert terminal["type"] == "session.next.step.ended"
    assert mock.count_for(marker) == 1
    assert [
        event["type"] for event in v2_events(client, session_id)
    ] == NORMAL_TURN_TYPES

    duplicate = v2_prompt(
        client, session_id, marker, message_id=message_id, resume=True
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["promotedSeq"] == 2
    assert mock.count_for(marker) == 1
    assert len(v2_events(client, session_id)) == len(NORMAL_TURN_TYPES)


def test_v2_busy_prompt_admission_before_previous_step_end(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_id = str(v2_create(client, env)["id"])
    first = new_marker("busy1")
    second = new_marker("busy2")

    assert v2_prompt(client, session_id, f"SLOW {first}").status_code == 200
    mock.wait_for_marker(first, 1, timeout=20)
    assert v2_prompt(client, session_id, second).status_code == 200

    deadline = time.time() + 40
    while time.time() < deadline:
        types = [event["type"] for event in v2_events(client, session_id)]
        if types.count("session.next.step.ended") >= 2:
            break
        time.sleep(0.05)
    else:
        raise TimeoutError(f"两轮未完成：{types[-12:]}")

    events = v2_events(client, session_id)
    types = [event["type"] for event in events]
    assert types.count("session.next.prompt.admitted") == 2
    first_end = types.index("session.next.step.ended")
    second_admit = [
        index
        for index, event_type in enumerate(types)
        if event_type == "session.next.prompt.admitted"
    ][1]
    second_prompt = [
        index
        for index, event_type in enumerate(types)
        if event_type == "session.next.prompted"
    ][1]
    assert second_admit < first_end < second_prompt

    assert mock.count_for(first) == 1
    assert mock.count_for(second) == 1
    steered = user_texts(mock.request_for(second))
    assert steered[:2] == [f"SLOW {first}", second]

    messages = v2_messages(client, session_id)
    assert [message["type"] for message in messages] == [
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert [message["text"] for message in messages if message["type"] == "user"] == [
        second,
        f"SLOW {first}",
    ]


def test_v2_invalid_model_and_agent_at_create(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    # 非法 model：create 接受并写进 session；prompt 仍 200，但回合静默停住。
    bad_model = v2_create(client, env, model=("mock", "does-not-exist"))
    bad_model_id = str(bad_model["id"])
    assert bad_model["model"]["id"] == "does-not-exist"
    marker = new_marker("badmodel")
    assert v2_prompt(client, bad_model_id, marker).status_code == 200
    time.sleep(2.5)
    assert mock.count_for(marker) == 0
    assert [event["type"] for event in v2_events(client, bad_model_id)] == [
        "session.next.prompt.admitted",
        "session.next.prompted",
    ]

    # 非法 agent：create 接受；prompt 仍按现有 agent 执行，但 session 保留脏值。
    bad_agent = v2_create(client, env, agent="does-not-exist")
    bad_agent_id = str(bad_agent["id"])
    assert bad_agent["agent"] == "does-not-exist"
    agent_marker = new_marker("badagent")
    terminal = v2_turn(client, bad_agent_id, agent_marker)
    assert terminal["type"] == "session.next.step.ended"
    assert mock.count_for(agent_marker) == 1
    assert v2_session(client, bad_agent_id)["agent"] == "does-not-exist"


def test_v2_location_readiness_requires_retry_not_global_catalog(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    catalog = client.get("/api/model")
    assert catalog.status_code == 200
    assert isinstance(catalog.json()["data"], list)

    cold = env.work_dir / "cold-location"
    cold.mkdir(exist_ok=True)
    # 新的 location 服务是异步启动的；全局 catalog 就绪不代表该 location 可执行。
    warmed = warm_location(client, env, mock, cold)
    assert warmed.startswith("ses_")
    session_id = str(v2_create(client, env, directory=cold)["id"])
    marker = new_marker("cold")
    terminal = v2_turn(client, session_id, marker)
    assert terminal["type"] == "session.next.step.ended"
    assert mock.count_for(marker) == 1


def test_v2_restart_keeps_history_and_after_cursor(
    standalone_env: Any, mock: MockProvider
) -> None:
    probe_env = standalone_env()
    client = probe_env.client()
    try:
        warm_location(client, probe_env, mock, probe_env.work_dir)
        session_id = str(v2_create(client, probe_env)["id"])
        first = new_marker("restart1")
        second = new_marker("restart2")
        v2_turn(client, session_id, first)
        v2_turn(client, session_id, second)

        events = v2_events(client, session_id)
        history_ids = [event["id"] for event in events]
        message_ids = [message["id"] for message in v2_messages(client, session_id)]
        cut = int(events[2]["durable"]["seq"])
        expected_after = [event for event in events if event["durable"]["seq"] > cut]

        probe_env.restart()
        client.close()
        client = probe_env.client()
        health = client.get("/global/health")
        assert health.status_code == 200
        assert "1.17.13" in health.text

        restarted = v2_events(client, session_id)
        assert [event["id"] for event in restarted] == history_ids
        after = v2_events(client, session_id, after=cut, limit=100)
        assert [event["id"] for event in after] == [
            event["id"] for event in expected_after
        ]
        assert [message["id"] for message in v2_messages(client, session_id)] == (
            message_ids
        )
        assert v2_session(client, session_id)["model"]["id"] == "mock-model"

        # 重启后 location 服务同样异步；先预热另一个会话，再验证原会话可继续。
        warm_location(client, probe_env, mock, probe_env.work_dir)
        third = new_marker("restart3")
        terminal = v2_turn(client, session_id, third)
        assert terminal["type"] == "session.next.step.ended"
        assert mock.count_for(third) == 1
        assert user_texts(mock.request_for(third)) == [first, second, third]
        assert len(v2_events(client, session_id)) > len(events)
    finally:
        client.close()


# ---------------------------------------- 会话级配置、只读接口与跨会话隔离
def test_p01_version_and_package_identity(env: ProbeEnv, client: httpx.Client) -> None:
    info = env.binary_info
    assert info is not None
    assert info.version == "1.17.13"
    assert info.package_name == "opencode-ai"
    assert info.package_version == "1.17.13"
    health = client.get("/global/health")
    assert health.status_code == 200
    assert health.json()["version"] == "1.17.13"

    # v2 自己的健康检查不带版本；版本只能从 /global/health 取。
    v2_health = client.get("/api/health")
    assert v2_health.status_code == 200
    assert v2_health.json() == {"healthy": True}

    location = v2_location(client)
    assert location["directory"] == env.server_directory(client)
    assert location["project"]["id"] == "global"

    # §3.9 只读端点存在性：统一使用 location[directory]，返回 {location, data} JSON。
    for path in ("/api/provider", "/api/command", "/api/fs/list", "/api/pty"):
        response = client.get(path, params={"location[directory]": str(env.work_dir)})
        assert response.status_code == 200, f"{path}: {response.text}"
        content_type = response.headers.get("content-type", "")
        assert "application/json" in content_type, f"{path}: {content_type}"
        assert isinstance(response.json()["data"], list), path


def test_p02_session_agent_switch(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_id = str(v2_create(client, env)["id"])
    agent_ids = {str(agent["id"]) for agent in v2_agents(client, env.work_dir)}
    assert {"build", "probe-a", "probe-b"} <= agent_ids

    v2_switch_agent(client, session_id, "probe-a")
    marker_a = new_marker("p02-a")
    v2_turn(client, session_id, marker_a)
    system_a = system_text(mock.request_for(marker_a))
    assert AGENT_A_PROMPT in system_a
    assert AGENT_B_PROMPT not in system_a

    v2_switch_agent(client, session_id, "probe-b")
    marker_b = new_marker("p02-b")
    v2_turn(client, session_id, marker_b)
    system_b = system_text(mock.request_for(marker_b))
    assert AGENT_B_PROMPT in system_b
    assert AGENT_A_PROMPT not in system_b

    v2_switch_agent(client, session_id, "build")
    marker_build = new_marker("p02-build")
    v2_turn(client, session_id, marker_build)
    system_build = system_text(mock.request_for(marker_build))
    assert AGENT_A_PROMPT not in system_build
    assert AGENT_B_PROMPT not in system_build
    assert v2_session(client, session_id)["agent"] == "build"

    types = [event["type"] for event in v2_events(client, session_id)]
    assert types.count("session.next.agent.switched") == 3


def test_p03_session_model_switch(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_id = str(v2_create(client, env)["id"])
    assert v2_session(client, session_id)["model"]["id"] == "mock-model"

    v2_switch_model(client, session_id, MODEL_B)
    marker_b = new_marker("p03-b")
    v2_turn(client, session_id, marker_b)
    assert mock.request_for(marker_b)["model"] == "mock-model-b"

    marker_sticky = new_marker("p03-sticky")
    v2_turn(client, session_id, marker_sticky)
    assert mock.request_for(marker_sticky)["model"] == "mock-model-b"

    v2_switch_model(client, session_id, MODEL)
    marker_default = new_marker("p03-default")
    v2_turn(client, session_id, marker_default)
    assert mock.request_for(marker_default)["model"] == "mock-model"
    assert v2_session(client, session_id)["model"]["id"] == "mock-model"

    # 新 session 不继承上一会话的 switch；create 显式 model 生效。
    other_id = str(v2_create(client, env)["id"])
    marker_other = new_marker("p03-other")
    v2_turn(client, other_id, marker_other)
    assert mock.request_for(marker_other)["model"] == "mock-model"

    explicit = v2_create(client, env, model=MODEL_B)
    assert explicit["model"]["id"] == "mock-model-b"
    marker_explicit = new_marker("p03-explicit")
    v2_turn(client, str(explicit["id"]), marker_explicit)
    assert mock.request_for(marker_explicit)["model"] == "mock-model-b"


def test_p05_v2_default_toolset_and_sanitize(
    env: ProbeEnv,
    client: httpx.Client,
    mock: MockProvider,
    v2_baseline_tools: set[str],
) -> None:
    session_id = str(v2_create(client, env)["id"])
    marker = new_marker("p05")
    v2_turn(client, session_id, marker)
    actual = set(mock.tool_names_for(marker))
    assert actual == v2_baseline_tools

    assert {
        "apply_patch",
        "bash",
        "edit",
        "glob",
        "grep",
        "read",
        "skill",
        "todowrite",
        "webfetch",
        "websearch",
        "write",
    } <= actual
    # v2 默认工具集与 legacy 不同：没有 task，也没有 MCP 工具。
    assert "task" not in actual
    assert not (MCP_TOOL_NAMES & actual)
    assert not (RAW_MCP_NAMES & actual)


def test_p06_prompt_tools_field_ignored(
    env: ProbeEnv,
    client: httpx.Client,
    mock: MockProvider,
    v2_baseline_tools: set[str],
) -> None:
    session_id = str(v2_create(client, env)["id"])
    marker = new_marker("p06")
    before = v2_last_seq(client, session_id)
    body = {
        "prompt": {"text": marker},
        "tools": {"bash": False, "skill": False},
    }
    response = client.post(f"/api/session/{session_id}/prompt", json=body)
    # v2 prompt 没有 tools 字段：请求被接受，但工具集不变。
    assert response.status_code == 200, response.text
    v2_wait_turn(client, session_id, before)
    assert set(mock.tool_names_for(marker)) == v2_baseline_tools

    marker_next = new_marker("p06-next")
    v2_turn(client, session_id, marker_next)
    assert set(mock.tool_names_for(marker_next)) == v2_baseline_tools


def test_p07_mcp_tools_absent_from_v2(
    env: ProbeEnv,
    client: httpx.Client,
    mock: MockProvider,
    v2_baseline_tools: set[str],
) -> None:
    # MCP 连接本身是好的（legacy 只读接口可证），但 v2 不暴露 MCP 工具。
    assert env.mcp_status(client)["probe"]["status"] == "connected"
    for path in ("/api/mcp", "/api/config"):
        fallback = client.get(path, params={"location[directory]": str(env.work_dir)})
        assert fallback.status_code == 200, path
        assert "text/html" in fallback.headers.get("content-type", ""), path
    assert not (MCP_TOOL_NAMES & v2_baseline_tools)

    session_id = str(v2_create(client, env)["id"])
    marker = new_marker("p07")
    assert (
        v2_prompt(client, session_id, f"CALLTOOL probe_echo_upper {marker}").status_code
        == 200
    )
    mock.wait_for_marker(marker, 2, timeout=30)
    assert not (MCP_TOOL_NAMES & set(mock.tool_names_for(marker)))
    results = mock.tool_results_for(marker)
    assert any("Unknown tool: probe_echo_upper" in result for result in results), (
        results
    )


def test_p08_skill_exposure(
    env: ProbeEnv,
    client: httpx.Client,
    mock: MockProvider,
    v2_baseline_tools: set[str],
) -> None:
    response = client.get(
        "/api/skill", params={"location[directory]": str(env.work_dir)}
    )
    assert response.status_code == 200, response.text
    skills = list(response.json()["data"])
    assert any(skill.get("name") == SKILL_NAME for skill in skills)
    assert "skill" in v2_baseline_tools

    session_id = str(v2_create(client, env)["id"])
    marker = new_marker("p08")
    assert v2_prompt(client, session_id, f"CALLTOOL skill {marker}").status_code == 200
    mock.wait_for_marker(marker, 2, timeout=30)
    results = mock.tool_results_for(marker)
    assert any(SKILL_BODY in result for result in results), results


def test_p14_restart_boundary(standalone_env: Any, mock: MockProvider) -> None:
    env = standalone_env()
    client = env.client()
    try:
        warm_location(client, env, mock, env.work_dir)
        session = v2_create(client, env, model=MODEL_B, agent="probe-a")
        session_id = str(session["id"])

        first = new_marker("p14-1")
        v2_turn(client, session_id, first)
        body = mock.request_for(first)
        assert body["model"] == "mock-model-b"
        assert AGENT_A_PROMPT in system_text(body)
        before_ids = [message["id"] for message in v2_messages(client, session_id)]

        env.restart()
        client.close()
        client = env.client()
        assert "1.17.13" in client.get("/global/health").text
        restored = v2_session(client, session_id)
        assert restored["model"]["id"] == "mock-model-b"
        assert restored["agent"] == "probe-a"
        assert [message["id"] for message in v2_messages(client, session_id)] == (
            before_ids
        )

        warm_location(client, env, mock, env.work_dir)
        second = new_marker("p14-2")
        v2_turn(client, session_id, second)
        body = mock.request_for(second)
        assert body["model"] == "mock-model-b"
        assert AGENT_A_PROMPT in system_text(body)
        assert user_texts(body) == [first, second]
    finally:
        client.close()


def test_p15_restart_id_stability(standalone_env: Any, mock: MockProvider) -> None:
    env = standalone_env()
    client = env.client()
    try:
        warm_location(client, env, mock, env.work_dir)
        session_id = str(v2_create(client, env)["id"])
        for index in range(2):
            v2_turn(client, session_id, new_marker(f"p15-{index}"))

        before = [
            (message["id"], part_ids(message))
            for message in v2_messages(client, session_id)
        ]
        env.restart()
        client.close()
        client = env.client()
        after = [
            (message["id"], part_ids(message))
            for message in v2_messages(client, session_id)
        ]
        assert after == before, (
            f"重启后 message/part ID 应保持一致；before={before}, after={after}"
        )
        assert len({message_id for message_id, _ in after}) == len(after)

        warm_location(client, env, mock, env.work_dir)
        v2_turn(client, session_id, new_marker("p15-3"))
        messages = v2_messages(client, session_id)
        assert len(messages) == 6
        new_ids = {message["id"] for message in messages[:2]}
        assert new_ids.isdisjoint({message_id for message_id, _ in before})
    finally:
        client.close()


def test_p16_tools_boundaries_ignored(
    env: ProbeEnv,
    client: httpx.Client,
    mock: MockProvider,
    v2_baseline_tools: set[str],
) -> None:
    session_id = str(v2_create(client, env)["id"])
    cases: list[tuple[dict[str, bool], str]] = [
        ({}, "空对象"),
        ({"unknown_tool_xyz": False}, "未知 key"),
        ({"echo_upper": False}, "原始 MCP 名"),
        ({"bash": False, "skill": False}, "禁用内建工具"),
        ({"probe_echo_upper": True}, "MCP 工具显式恢复"),
    ]
    for tools, context in cases:
        marker = new_marker("p16")
        before = v2_last_seq(client, session_id)
        response = client.post(
            f"/api/session/{session_id}/prompt",
            json={"prompt": {"text": marker}, "tools": tools},
        )
        assert response.status_code == 200, (context, response.text)
        v2_wait_turn(client, session_id, before)
        actual = set(mock.tool_names_for(marker))
        assert actual == v2_baseline_tools, f"{context}: {sorted(actual)}"

    # create 也不接受 tools：字段被丢弃，工具集回到 baseline。
    created = v2_create(client, env, tools={"bash": False})
    assert "tools" not in created
    marker = new_marker("p16-create")
    v2_turn(client, str(created["id"]), marker)
    assert set(mock.tool_names_for(marker)) == v2_baseline_tools


def test_p17_default_location(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    default = v2_location(client)
    directory = str(default["directory"])
    assert directory == env.server_directory(client)

    warm_location(client, env, mock, directory)
    response = client.post("/api/session", json={})
    assert response.status_code == 200, response.text
    session = dict(response.json()["data"])
    assert session["location"]["directory"] == directory

    marker = new_marker("p17")
    assert (
        v2_prompt(client, str(session["id"]), f"CALLTOOL bash pwd {marker}").status_code
        == 200
    )
    mock.wait_for_marker(marker, 2, timeout=30)
    results = mock.tool_results_for(marker)
    assert any(directory in result for result in results), results

    # create 不校验 directory 是否存在：不存在的路径也返回 200。
    missing = env.work_dir / "missing-location"
    assert not missing.exists()
    accepted = client.post(
        "/api/session", json={"location": {"directory": str(missing)}}
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["data"]["location"]["directory"] == str(missing)


def test_p18_invalid_switch_recovery(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    # 非法 model：switch 204 并写进 session；prompt 200 但回合静默停在 prompted。
    session_id = str(v2_create(client, env)["id"])
    v2_switch_model(client, session_id, ("mock", "nope-model"))
    assert v2_session(client, session_id)["model"]["id"] == "nope-model"
    marker = new_marker("p18-model")
    assert v2_prompt(client, session_id, marker).status_code == 200
    time.sleep(2.5)
    assert mock.count_for(marker) == 0
    types = [event["type"] for event in v2_events(client, session_id)]
    assert types == [
        "session.next.model.switched",
        "session.next.prompt.admitted",
        "session.next.prompted",
    ]

    # 切回合法 model 可恢复，无需重建会话。
    v2_switch_model(client, session_id, MODEL)
    recover = new_marker("p18-model-recover")
    v2_turn(client, session_id, recover)
    assert mock.count_for(recover) == 1
    assert mock.request_for(recover)["model"] == "mock-model"

    # 非法 agent：switch 204 并保留脏值；回合按默认 agent 继续执行。
    bad_agent_id = str(v2_create(client, env)["id"])
    v2_switch_agent(client, bad_agent_id, "does-not-exist")
    assert v2_session(client, bad_agent_id)["agent"] == "does-not-exist"
    marker = new_marker("p18-agent")
    terminal = v2_turn(client, bad_agent_id, marker)
    assert terminal["type"] == "session.next.step.ended"
    assert mock.count_for(marker) == 1
    system = system_text(mock.request_for(marker))
    assert AGENT_A_PROMPT not in system
    assert AGENT_B_PROMPT not in system
    assert v2_session(client, bad_agent_id)["agent"] == "does-not-exist"


def test_p19_provider_failure_is_bounded(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    def observe(label: str, expected_requests: int, expected: str) -> None:
        session_id = str(v2_create(client, env)["id"])
        marker = new_marker(label.lower())
        assert v2_prompt(client, session_id, f"{label} {marker}").status_code == 200
        mock.wait_for_marker(marker, 1, timeout=30)

        deadline = time.time() + 40
        terminal: dict[str, Any] | None = None
        while time.time() < deadline:
            terminal = next(
                (
                    event
                    for event in v2_events(client, session_id)
                    if event["type"]
                    in {"session.next.step.failed", "session.next.step.ended"}
                ),
                None,
            )
            if terminal is not None:
                break
            time.sleep(0.2)
        assert terminal is not None, f"{label}: 40s 内没有终态事件"
        assert terminal["type"] == "session.next.step.failed"
        assert expected in terminal["data"]["error"]["message"]
        # 1.17.13 的重试次数是固定的：ERROR500 精确 3 次，TIMEOUT 1 次。
        assert mock.count_for(marker) == expected_requests
        v2_wait_idle(client, session_id)

        types = [event["type"] for event in v2_events(client, session_id)]
        assert "session.error" not in types
        assistant = next(
            message
            for message in v2_messages(client, session_id)
            if message["type"] == "assistant"
        )
        assert assistant["finish"] == "error"
        assert assistant["content"] == []

    observe("ERROR500", 3, "HTTP 500")
    observe("TIMEOUT", 1, "transport")


def test_p21_cross_session_isolation(
    env: ProbeEnv, client: httpx.Client, mock: MockProvider
) -> None:
    session_a = str(v2_create(client, env)["id"])
    session_b = str(v2_create(client, env)["id"])
    v2_switch_agent(client, session_a, "probe-a")
    v2_switch_model(client, session_a, MODEL_B)

    marker_a = new_marker("p21-a")
    v2_turn(client, session_a, marker_a)
    body_a = mock.request_for(marker_a)
    assert body_a["model"] == "mock-model-b"
    assert AGENT_A_PROMPT in system_text(body_a)

    marker_b = new_marker("p21-b")
    v2_turn(client, session_b, marker_b)
    body_b = mock.request_for(marker_b)
    assert body_b["model"] == "mock-model"
    assert AGENT_A_PROMPT not in system_text(body_b)
    assert AGENT_B_PROMPT not in system_text(body_b)
    assert v2_session(client, session_b).get("agent") != "probe-a"

    types_a = [event["type"] for event in v2_events(client, session_a)]
    types_b = [event["type"] for event in v2_events(client, session_b)]
    assert "session.next.agent.switched" in types_a
    assert "session.next.model.switched" in types_a
    assert "session.next.agent.switched" not in types_b
    assert "session.next.model.switched" not in types_b

    v2_switch_agent(client, session_b, "probe-b")
    marker_b2 = new_marker("p21-b2")
    v2_turn(client, session_b, marker_b2)
    assert AGENT_B_PROMPT in system_text(mock.request_for(marker_b2))

    marker_a2 = new_marker("p21-a2")
    v2_turn(client, session_a, marker_a2)
    body_a2 = mock.request_for(marker_a2)
    assert body_a2["model"] == "mock-model-b"
    assert AGENT_A_PROMPT in system_text(body_a2)
