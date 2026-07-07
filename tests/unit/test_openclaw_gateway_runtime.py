from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from witty_agent_server.infra.clients.base import ClientBase
from witty_agent_server.runtimes.openclaw_gateway_runtime import OpenClawGatewayRuntime


class StubGatewayClient(ClientBase):
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def list_agents(self) -> dict[str, Any]:
        return {}

    def list_sessions(self, *, agent_id: str) -> dict[str, Any]:
        return {"sessions": []}

    def get_agent(self, *, agent_id: str) -> dict[str, Any] | None:
        return None

    def get_skills_status(self, *, agent_id: str | None = None) -> dict[str, Any]:
        return {}

    def create_session(self, *, session_key: str) -> None:
        return None

    def delete_session(self, *, session_key: str) -> None:
        return None

    def abort_session(self, *, session_key: str) -> None:
        return None

    def stream_turn(
        self, *, session_key: str, message: str
    ) -> Iterator[dict[str, Any]]:
        del session_key, message
        yield from self._events


@pytest.mark.parametrize(
    ("payload", "expected_session_id"),
    [
        ({"sessionId": "runtime-session-top"}, "runtime-session-top"),
        ({"data": {"sessionId": "runtime-session-data"}}, "runtime-session-data"),
        (
            {"session": {"sessionId": "runtime-session-nested"}},
            "runtime-session-nested",
        ),
    ],
)
def test_run_turn_maps_sessions_changed_to_runtime_identity_event(
    payload: dict[str, Any],
    expected_session_id: str,
) -> None:
    runtime = OpenClawGatewayRuntime(
        client=StubGatewayClient(
            [
                {
                    "type": "sessions.changed",
                    "payload": payload,
                }
            ]
        )
    )

    events = list(runtime.run_turn(session_key="session-key", message="hello"))

    assert events == [
        {
            "type": "session.runtime.changed",
            "payload": {"runtime_session_id": expected_session_id},
        }
    ]


def test_run_turn_skips_sessions_changed_without_runtime_session_id() -> None:
    runtime = OpenClawGatewayRuntime(
        client=StubGatewayClient(
            [
                {
                    "type": "sessions.changed",
                    "payload": {},
                }
            ]
        )
    )

    events = list(runtime.run_turn(session_key="session-key", message="hello"))

    assert events == []
