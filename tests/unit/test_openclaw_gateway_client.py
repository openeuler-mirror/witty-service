from __future__ import annotations

from collections.abc import Iterator
import logging
from typing import Any

import pytest

from witty_agent_server.infra.clients.openclaw_gateway_client import (
    OpenClawGatewayClient,
)


class DummyConnection:
    def __enter__(self) -> DummyConnection:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        return None


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
def test_collect_stream_events_keeps_sessions_changed_with_runtime_session_id(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, Any],
    expected_session_id: str,
) -> None:
    client = OpenClawGatewayClient(token="token")
    messages = iter(
        [
            {
                "type": "event",
                "event": "sessions.changed",
                "payload": payload,
            }
        ]
    )

    def fake_recv_json(ws: Any, *, timeout: float) -> dict[str, Any]:
        del ws, timeout
        try:
            return next(messages)
        except StopIteration as exc:
            raise TimeoutError from exc

    monkeypatch.setattr(client, "_recv_json", fake_recv_json)

    events = list(
        client._collect_stream_events(
            ws=object(),
            session_key="session-key",
            run_id=None,
        )
    )

    assert events == [
        {
            "type": "sessions.changed",
            "payload": payload,
        }
    ]
    assert client._extract_runtime_session_id(payload) == expected_session_id


def test_collect_stream_events_drops_sessions_changed_when_session_key_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OpenClawGatewayClient(token="token")
    messages = iter(
        [
            {
                "type": "event",
                "event": "sessions.changed",
                "payload": {
                    "sessionKey": "other-session-key",
                    "sessionId": "runtime-session-1",
                },
            }
        ]
    )

    def fake_recv_json(ws: Any, *, timeout: float) -> dict[str, Any]:
        del ws, timeout
        try:
            return next(messages)
        except StopIteration as exc:
            raise TimeoutError from exc

    monkeypatch.setattr(client, "_recv_json", fake_recv_json)

    events = list(
        client._collect_stream_events(
            ws=object(),
            session_key="session-key",
            run_id=None,
        )
    )

    assert events == []


def test_collect_stream_events_skips_sessions_changed_without_runtime_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OpenClawGatewayClient(token="token")
    messages = iter(
        [
            {
                "type": "event",
                "event": "sessions.changed",
                "payload": {},
            }
        ]
    )

    def fake_recv_json(ws: Any, *, timeout: float) -> dict[str, Any]:
        del ws, timeout
        try:
            return next(messages)
        except StopIteration as exc:
            raise TimeoutError from exc

    monkeypatch.setattr(client, "_recv_json", fake_recv_json)

    events = list(
        client._collect_stream_events(
            ws=object(),
            session_key="session-key",
            run_id=None,
        )
    )

    assert events == []


def test_stream_turn_subscribes_session_change_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = OpenClawGatewayClient(token="token")
    subscription_calls: list[str] = []

    def fake_open_connection() -> DummyConnection:
        return DummyConnection()

    def fake_rpc(ws: Any, *, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del ws, params
        if method == "sessions.send":
            return {"runId": "run-1"}
        return {}

    def fake_ensure_session_change_streaming(*, ws: Any) -> None:
        del ws
        subscription_calls.append("session-change")

    def fake_ensure_tool_output_streaming(*, ws: Any, session_key: str) -> None:
        del ws, session_key

    def fake_collect_stream_events(
        ws: Any,
        *,
        session_key: str,
        run_id: str | None,
    ) -> Iterator[dict[str, Any]]:
        del ws, session_key, run_id
        return iter(())

    monkeypatch.setattr(client, "_open_connection", fake_open_connection)
    monkeypatch.setattr(client, "_rpc", fake_rpc)
    monkeypatch.setattr(
        client,
        "_ensure_session_change_streaming",
        fake_ensure_session_change_streaming,
    )
    monkeypatch.setattr(
        client,
        "_ensure_tool_output_streaming",
        fake_ensure_tool_output_streaming,
    )
    monkeypatch.setattr(client, "_collect_stream_events", fake_collect_stream_events)

    events = list(client.stream_turn(session_key="session-key", message="hello"))

    assert events == []
    assert subscription_calls == ["session-change"]
