from __future__ import annotations

from typing import Any

import pytest

from witty_agent_server.infra.clients.base import ClientBase
from witty_agent_server.runtimes.opencode_runtime import OpenCodeRuntime


class _RecordingClient(ClientBase):
    """记录所有调用，便于验证 OpenCodeRuntime 的转调。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._session_map: dict[str, str] = {}

    def list_agents(self) -> dict[str, Any]:
        self.calls.append(("list_agents", {}))
        return {"defaultId": "main", "agents": []}

    def list_sessions(self, *, agent_id: str) -> dict[str, Any]:
        self.calls.append(("list_sessions", {"agent_id": agent_id}))
        return {"sessions": [{"id": "s1"}, {"id": "s2"}]}

    def get_agent(self, *, agent_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def get_skills_status(self, *, agent_id: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def create_session(self, *, session_key: str) -> None:
        self.calls.append(("create_session", {"session_key": session_key}))
        self._session_map[session_key] = f"opencode-id-{session_key}"

    def delete_session(self, *, session_key: str) -> None:
        self.calls.append(("delete_session", {"session_key": session_key}))
        self._session_map.pop(session_key, None)

    def abort_session(self, *, session_key: str) -> None:
        self.calls.append(("abort_session", {"session_key": session_key}))

    def stream_turn(self, *, session_key: str, message: str):
        raise NotImplementedError


@pytest.mark.parametrize(
    "method, session_key",
    [
        ("create_session", "k1"),
        ("delete_session", "k2"),
        ("abort_session", "k3"),
    ],
)
def test_runtime_session_ops_delegate_to_client(method: str, session_key: str) -> None:
    client = _RecordingClient()
    runtime = OpenCodeRuntime(client=client)

    getattr(runtime, method)(session_key=session_key)

    assert client.calls == [(method, {"session_key": session_key})]


def test_runtime_list_sessions_delegates_and_unwraps_sessions_list() -> None:
    client = _RecordingClient()
    runtime = OpenCodeRuntime(client=client)

    result = runtime.list_sessions(agent_id="ignored")

    assert client.calls == [("list_sessions", {"agent_id": "ignored"})]
    assert result == [{"id": "s1"}, {"id": "s2"}]


def test_runtime_list_sessions_returns_empty_when_payload_missing() -> None:
    class _EmptyClient(_RecordingClient):
        def list_sessions(self, *, agent_id: str) -> dict[str, Any]:
            return {}  # 没有 sessions key

    client = _EmptyClient()
    runtime = OpenCodeRuntime(client=client)

    assert runtime.list_sessions(agent_id="x") == []


def test_runtime_without_client_raises_on_session_ops() -> None:
    runtime = OpenCodeRuntime(client=None)

    with pytest.raises(RuntimeError):
        runtime.create_session(session_key="k")
    with pytest.raises(RuntimeError):
        runtime.delete_session(session_key="k")
    with pytest.raises(RuntimeError):
        runtime.abort_session(session_key="k")
    with pytest.raises(RuntimeError):
        runtime.list_sessions(agent_id="x")


# =============================================================================
# answer_question / reject_question — delegation
# =============================================================================


def test_answer_question_delegates_to_opencode_client() -> None:
    from witty_agent_server.infra.clients.opencode_client import OpenCodeClient

    client = OpenCodeClient()
    client._http_client = _FakeHttpClient()

    runtime = OpenCodeRuntime(client=client)
    result = runtime.answer_question(request_id="que_1", answers=[["yes"]])

    assert result is True
    assert client._http_client.calls == [
        ("POST", "/question/que_1/reply", {"answers": [["yes"]]}),
    ]


def test_reject_question_delegates_to_opencode_client() -> None:
    from witty_agent_server.infra.clients.opencode_client import OpenCodeClient

    client = OpenCodeClient()
    client._http_client = _FakeHttpClient()

    runtime = OpenCodeRuntime(client=client)
    result = runtime.reject_question(request_id="que_2")

    assert result is True
    assert client._http_client.calls == [
        ("POST", "/question/que_2/reject", None),
    ]


def test_answer_question_with_non_opencode_client_raises_not_implemented() -> None:
    runtime = OpenCodeRuntime(client=_RecordingClient())

    with pytest.raises(NotImplementedError, match="question answering"):
        runtime.answer_question(request_id="q1", answers=[["a"]])


def test_reject_question_with_non_opencode_client_raises_not_implemented() -> None:
    runtime = OpenCodeRuntime(client=_RecordingClient())

    with pytest.raises(NotImplementedError, match="question rejection"):
        runtime.reject_question(request_id="q1")


# =============================================================================
# _map_question_asked / _map_question_replied / _map_question_rejected
# =============================================================================


def test_map_question_asked_returns_mapped_event() -> None:
    raw = {
        "type": "question.asked",
        "id": "que_abc",
        "sessionID": "ses_1",
        "questions": [{"question": "Do you approve?", "options": ["yes", "no"]}],
    }

    result = OpenCodeRuntime._map_question_asked(raw)

    assert result == {
        "type": "question.asked",
        "payload": {
            "question_id": "que_abc",
            "questions": [{"question": "Do you approve?", "options": ["yes", "no"]}],
        },
    }


def test_map_question_asked_missing_id_returns_none() -> None:
    raw = {"type": "question.asked", "questions": []}

    result = OpenCodeRuntime._map_question_asked(raw)

    assert result is None


@pytest.mark.parametrize("invalid_id", ["", None])
def test_map_question_asked_invalid_id_returns_none(invalid_id: object) -> None:
    raw = {"type": "question.asked", "id": invalid_id, "questions": []}

    result = OpenCodeRuntime._map_question_asked(raw)

    assert result is None


def test_map_question_replied_returns_mapped_event() -> None:
    raw = {
        "type": "question.replied",
        "sessionID": "ses_1",
        "requestID": "que_abc",
        "answers": [{"answer": "approved"}],
    }

    result = OpenCodeRuntime._map_question_replied(raw)

    assert result == {
        "type": "question.replied",
        "payload": {
            "request_id": "que_abc",
            "answers": [{"answer": "approved"}],
        },
    }


def test_map_question_rejected_returns_mapped_event() -> None:
    raw = {
        "type": "question.rejected",
        "sessionID": "ses_1",
        "requestID": "que_abc",
    }

    result = OpenCodeRuntime._map_question_rejected(raw)

    assert result == {
        "type": "question.rejected",
        "payload": {"request_id": "que_abc"},
    }


# =============================================================================
# _map_opencode_event — question events + STREAM_ERROR fallback
# =============================================================================


def test_map_opencode_event_question_asked_yields_event() -> None:
    raw = {"type": "question.asked", "id": "que_1", "questions": []}

    events = list(OpenCodeRuntime._map_opencode_event(raw))

    assert len(events) == 1
    assert events[0]["type"] == "question.asked"


def test_map_opencode_event_question_asked_missing_id_yields_stream_error() -> None:
    """畸形 question.asked 事件（缺少 id）应产出 STREAM_ERROR 而非静默丢弃。"""
    raw = {"type": "question.asked", "questions": []}

    events = list(OpenCodeRuntime._map_opencode_event(raw))

    assert len(events) == 1
    assert events[0]["type"] == "stream.error"
    assert events[0]["payload"]["error"] == raw


# =============================================================================
# write 工具 → artifact.* 事件
# =============================================================================


class _StreamingClient(_RecordingClient):
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def stream_turn(self, *, session_key: str, message: str):
        del session_key, message
        yield from self._events


def _write_part(
    *,
    status: str,
    output: str = "",
    is_error: bool = False,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "status": status,
        "input": {"file_path": "output/demo.html", "content": "<h1>hi</h1>"},
    }
    if status in ("completed", "error"):
        state["output"] = output
        state["metadata"] = {"exit": -1 if is_error else 0}
    elif status == "running" and output:
        state["metadata"] = {"output": output}
    return {
        "type": "message.part.updated",
        "part": {
            "type": "tool",
            "id": "part-1",
            "callID": "call-1",
            "tool": "write",
            "state": state,
        },
    }


def _write_turn_events(*, is_error: bool = False) -> list[dict[str, Any]]:
    runtime = OpenCodeRuntime(
        client=_StreamingClient(
            [
                _write_part(status="running"),
                _write_part(
                    status="error" if is_error else "completed",
                    output="boom" if is_error else "wrote output/demo.html",
                    is_error=is_error,
                ),
                {"type": "session.idle"},
            ]
        )
    )
    return list(runtime.run_turn(session_key="session-key", message="hello"))


def test_run_turn_emits_artifact_events_for_write_tool() -> None:
    events = _write_turn_events()

    assert [e["type"] for e in events] == [
        "tool.call.started",
        "artifact.started",
        "tool.call.response",
        "artifact.completed",
        "turn.completed",
    ]
    started = events[1]["payload"]
    assert started["id"] == "output/demo.html"
    assert started["status"] == "creating"
    assert "content" not in started
    completed = events[3]["payload"]
    assert completed["status"] == "ready"
    assert completed["content"] == "<h1>hi</h1>"
    assert completed["size"] == len("<h1>hi</h1>")


def test_run_turn_write_error_marks_artifact_error() -> None:
    events = _write_turn_events(is_error=True)

    assert events[3]["type"] == "artifact.completed"
    assert events[3]["payload"]["status"] == "error"
    assert "content" not in events[3]["payload"]


def test_run_turn_write_with_streaming_output_still_emits_artifact_events() -> None:
    # running 阶段即使带增量输出，也必须先发 started（否则拿不到 input，
    # 统一 artifact 钩子会丢失 artifact.* 事件）。
    runtime = OpenCodeRuntime(
        client=_StreamingClient(
            [
                _write_part(status="running", output="partial output"),
                _write_part(status="completed", output="wrote output/demo.html"),
                {"type": "session.idle"},
            ]
        )
    )
    events = list(runtime.run_turn(session_key="session-key", message="hello"))

    assert [e["type"] for e in events] == [
        "tool.call.started",
        "artifact.started",
        "tool.call.response",
        "artifact.completed",
        "turn.completed",
    ]
    completed = events[3]["payload"]
    assert completed["status"] == "ready"
    assert completed["content"] == "<h1>hi</h1>"


def test_run_turn_write_streams_delta_after_started() -> None:
    # 已发过 started 后，带增量输出的后续 running 事件应走 tool.call.delta。
    runtime = OpenCodeRuntime(
        client=_StreamingClient(
            [
                _write_part(status="running"),
                _write_part(status="running", output="partial output"),
                _write_part(status="completed", output="wrote output/demo.html"),
                {"type": "session.idle"},
            ]
        )
    )
    events = list(runtime.run_turn(session_key="session-key", message="hello"))

    assert [e["type"] for e in events] == [
        "tool.call.started",
        "artifact.started",
        "tool.call.delta",
        "tool.call.response",
        "artifact.completed",
        "turn.completed",
    ]


# =============================================================================
# Fake HTTP client for OpenCodeClient testing
# =============================================================================


class _FakeHttpClient:
    """伪造 httpx.Client，仅支持 answer_question / reject_question 的 POST 调用。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def post(self, url: str, json: object = None) -> _FakeResponse:
        self.calls.append(("POST", url, json))
        return _FakeResponse()

    def close(self) -> None:
        pass

    @property
    def is_closed(self) -> bool:
        return False


class _FakeResponse:
    status_code: int = 200
