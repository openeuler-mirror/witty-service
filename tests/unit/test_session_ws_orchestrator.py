from __future__ import annotations

from typing import Any

import pytest

from witty_agent_server.application.services.session_identity_store import (
    SessionIdentityStore,
)
from witty_agent_server.application.services.session_ws_orchestrator import (
    SessionWSOrchestrator,
    SessionWSOrchestratorError,
)
from witty_agent_server.runtimes.runtime_base import RuntimeBase


class DummyRuntime(RuntimeBase):
    runtime_type: Any = "test-runtime"

    def __init__(self) -> None:
        super().__init__(client=None)
        self._answer_question_result: bool = True
        self._reject_question_result: bool = True
        self.answer_question_calls: list[dict[str, Any]] = []
        self.reject_question_calls: list[dict[str, Any]] = []
        self._raise_on_answer: Exception | None = None
        self._raise_on_reject: Exception | None = None

    def _map_events(self, raw: dict[str, Any]) -> Any:
        raise NotImplementedError

    def answer_question(self, *, request_id: str, answers: list[list[str]]) -> bool:
        self.answer_question_calls.append({"request_id": request_id, "answers": answers})
        if self._raise_on_answer is not None:
            raise self._raise_on_answer
        return self._answer_question_result

    def reject_question(self, *, request_id: str) -> bool:
        self.reject_question_calls.append({"request_id": request_id})
        if self._raise_on_reject is not None:
            raise self._raise_on_reject
        return self._reject_question_result


class UnsupportedRuntime(RuntimeBase):
    """未覆盖 answer_question / reject_question 的 runtime，默认抛 NotImplementedError。"""
    runtime_type: Any = "unsupported-runtime"

    def _map_events(self, raw: dict[str, Any]) -> Any:
        raise NotImplementedError

    def _ensure_client(self) -> Any:
        raise RuntimeError("no client")


class DummySessionService:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._session: dict[str, Any] | None = None
        self._runtime_map: dict[str, RuntimeBase] = {}

    def append_event(
        self,
        *,
        agent_id: str,
        session_id: str,
        event: dict[str, Any],
    ) -> None:
        del agent_id, session_id
        self.events.append(event)

    def get_session(self, *, agent_id: str, session_id: str) -> dict[str, Any] | None:
        del agent_id, session_id
        return self._session

    def get_runtime(self, runtime_type: str) -> RuntimeBase | None:
        return self._runtime_map.get(runtime_type)


class DummyAgentService:
    pass


def _make_orchestrator(
    *,
    session: dict[str, Any] | None = None,
    runtime_type: str = "test-runtime",
    runtime: RuntimeBase | None = None,
) -> SessionWSOrchestrator:
    session_service = DummySessionService()
    session_service._session = session or {
        "runtime_type": runtime_type,
        "runtime_session_key": "agent:1:session:key-1",
    }
    session_service._runtime_map[runtime_type] = runtime or DummyRuntime()
    return SessionWSOrchestrator(
        session_service=session_service,
        agent_service=DummyAgentService(),
        identity_store=SessionIdentityStore(),
        runtime_type=runtime_type,
    )


def test_handle_runtime_event_forwards_runtime_identity_change() -> None:
    identity_store = SessionIdentityStore()
    orchestrator = SessionWSOrchestrator(
        session_service=DummySessionService(),
        agent_service=DummyAgentService(),
        identity_store=identity_store,
        runtime_type="openclaw",
    )
    identity = identity_store.bind(
        agent_id="agent-1",
        session_id="session-1",
        runtime_type="openclaw",
        runtime_session_key="agent:1:session:key-1",
        runtime_session_id=None,
    )

    events = list(
        orchestrator._handle_runtime_event(
            agent_id="agent-1",
            session_id="session-1",
            runtime_type="openclaw",
            identity=identity,
            event={
                "type": "session.runtime.changed",
                "payload": {"runtime_session_id": "runtime-session-1"},
            },
        )
    )

    assert len(events) == 1
    assert events[0]["type"] == "session.runtime.changed"
    assert events[0]["agent_id"] == "agent-1"
    assert events[0]["session_id"] == "session-1"
    assert events[0]["runtime_type"] == "openclaw"
    assert events[0]["payload"] == {
        "runtime_session_id": "runtime-session-1",
        "runtime_session_key": "agent:1:session:key-1",
    }
    resolved = identity_store.resolve(agent_id="agent-1", session_id="session-1")
    assert resolved is not None
    assert resolved.runtime_session_id == "runtime-session-1"


# =============================================================================
# answer_question / reject_question
# =============================================================================


def test_answer_question_delegates_to_runtime() -> None:
    runtime = DummyRuntime()
    orchestrator = _make_orchestrator(runtime=runtime)

    result = orchestrator.answer_question(
        agent_id="agent-1",
        session_id="session-1",
        request_id="que_1",
        answers=[["yes"]],
    )

    assert result is True
    assert runtime.answer_question_calls == [
        {"request_id": "que_1", "answers": [["yes"]]},
    ]


def test_reject_question_delegates_to_runtime() -> None:
    runtime = DummyRuntime()
    orchestrator = _make_orchestrator(runtime=runtime)

    result = orchestrator.reject_question(
        agent_id="agent-1",
        session_id="session-1",
        request_id="que_2",
    )

    assert result is True
    assert runtime.reject_question_calls == [{"request_id": "que_2"}]


def test_answer_question_not_supported_raises_runtime_not_supported() -> None:
    orchestrator = _make_orchestrator(
        runtime_type="unsupported-runtime",
        runtime=UnsupportedRuntime(),
    )

    with pytest.raises(SessionWSOrchestratorError) as exc:
        orchestrator.answer_question(
            agent_id="agent-1",
            session_id="session-1",
            request_id="q1",
            answers=[["a"]],
        )

    assert exc.value.code == "RUNTIME_NOT_SUPPORTED"
    assert exc.value.status_code == 400
    assert "unsupported-runtime" in exc.value.message


def test_reject_question_not_supported_raises_runtime_not_supported() -> None:
    orchestrator = _make_orchestrator(
        runtime_type="unsupported-runtime",
        runtime=UnsupportedRuntime(),
    )

    with pytest.raises(SessionWSOrchestratorError) as exc:
        orchestrator.reject_question(
            agent_id="agent-1",
            session_id="session-1",
            request_id="q1",
        )

    assert exc.value.code == "RUNTIME_NOT_SUPPORTED"
    assert exc.value.status_code == 400
    assert "unsupported-runtime" in exc.value.message


def test_answer_question_upstream_error_wraps_to_502() -> None:
    runtime = DummyRuntime()
    runtime._raise_on_answer = RuntimeError("connection refused")
    orchestrator = _make_orchestrator(runtime=runtime)

    with pytest.raises(SessionWSOrchestratorError) as exc:
        orchestrator.answer_question(
            agent_id="agent-1",
            session_id="session-1",
            request_id="q1",
            answers=[["a"]],
        )

    assert exc.value.code == "RUNTIME_UPSTREAM_ERROR"
    assert exc.value.status_code == 502
    assert "failed to send answer" in exc.value.message


# =============================================================================
# _infer_event_source — question events
# =============================================================================


@pytest.mark.parametrize(
    ("event_type", "expected_source"),
    [
        ("question.asked", "assistant"),
        ("question.replied", "assistant"),
        ("question.rejected", "assistant"),
    ],
)
def test_infer_event_source_question_events_are_assistant(
    event_type: str, expected_source: str
) -> None:
    orchestrator = _make_orchestrator()

    result = orchestrator._infer_event_source(event_type)

    assert result == expected_source
