from __future__ import annotations

from typing import Any

from witty_agent_server.application.services.session_identity_store import (
    SessionIdentityStore,
)
from witty_agent_server.application.services.session_ws_orchestrator import (
    SessionWSOrchestrator,
)


class DummySessionService:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append_event(
        self,
        *,
        agent_id: str,
        session_id: str,
        event: dict[str, Any],
    ) -> None:
        del agent_id, session_id
        self.events.append(event)


class DummyAgentService:
    pass


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
