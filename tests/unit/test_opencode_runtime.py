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
