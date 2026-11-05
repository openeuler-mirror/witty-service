from __future__ import annotations

from typing import Any

import pytest

from witty_agent_server.application.models.agent import AgentStatus
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
        self.answer_question_calls.append(
            {"request_id": request_id, "answers": answers}
        )
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
        runtime_type=runtime_type,
    )


def test_runtime_session_key_comes_from_session_record() -> None:
    """编排器不再维护独立身份存储：只读 session 记录里的派生键。"""
    orchestrator = _make_orchestrator()

    session = orchestrator._require_session(agent_id="agent-1", session_id="s-1")

    assert orchestrator._resolve_runtime_session_key(session=session) == (
        "agent:1:session:key-1"
    )


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


# =============================================================================
# precheck_message — 单 runtime 事实不受环境影响
# =============================================================================


class _RunningAgent:
    status = AgentStatus.RUNNING


def _make_running_orchestrator(
    *,
    session_runtime_type: str,
    runtime_type: str = "opencode",
) -> SessionWSOrchestrator:
    session_service = DummySessionService()
    session_service._session = {
        "runtime_type": session_runtime_type,
        "runtime_session_key": "agent:1:session:key-1",
    }
    session_service._runtime_map[runtime_type] = DummyRuntime()
    agent_service = DummyAgentService()
    agent_service.agent = _RunningAgent()  # type: ignore[attr-defined]
    return SessionWSOrchestrator(
        session_service=session_service,
        agent_service=agent_service,
        runtime_type=runtime_type,
    )


def test_precheck_ignores_legacy_runtime_environment(monkeypatch) -> None:
    """旧 WITTY_RUNTIME_SUPPORTED=openclaw 不得把真实 OpenCode 会话判成不可用。"""
    monkeypatch.setenv("WITTY_RUNTIME_DEFAULT", "openclaw")
    monkeypatch.setenv("WITTY_RUNTIME_SUPPORTED", "openclaw")
    orchestrator = _make_running_orchestrator(session_runtime_type="opencode")

    orchestrator.precheck_message(agent_id="agent-1", session_id="s-1", message="hi")


def test_precheck_rejects_retired_runtime_session() -> None:
    """历史 Session 来自已退役 Runtime：明确 400，而不是 503 掩盖成暂时不可用。"""
    orchestrator = _make_running_orchestrator(session_runtime_type="openclaw")

    with pytest.raises(SessionWSOrchestratorError) as exc:
        orchestrator.precheck_message(
            agent_id="agent-1", session_id="s-1", message="hi"
        )

    assert exc.value.code == "UNSUPPORTED_RUNTIME_TYPE"
    assert exc.value.status_code == 400
    assert "openclaw" in exc.value.message
