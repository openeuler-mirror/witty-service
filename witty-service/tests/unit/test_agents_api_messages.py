from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from src.main import create_app


def test_send_message_returns_sandbox_type_and_events(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "test-token")

    manager = MagicMock()
    manager.send_message = AsyncMock(
        return_value={
            "sandbox_type": "local_process",
            "events": [
                {
                    "type": "message.delta",
                    "session_id": "session-1",
                    "runtime_type": "local_process",
                    "event_id": "evt-1",
                    "ts_ms": 123,
                    "payload": {"delta": "hello"},
                }
            ],
        }
    )

    services = MagicMock()
    services.get_agent_manager_for_agent.return_value = manager

    client = TestClient(create_app(services=services))

    resp = client.post(
        "/api/v1/agents/agent-1/sessions/session-1/messages",
        headers={"Authorization": "Bearer test-token"},
        json={"content": "hello"},
    )

    assert resp.status_code == 200
    assert resp.json() == {
        "sandbox_type": "local_process",
        "events": [
            {
                "type": "message.delta",
                "session_id": "session-1",
                "runtime_type": "local_process",
                "event_id": "evt-1",
                "ts_ms": 123,
                "payload": {"delta": "hello"},
            }
        ],
    }
    manager.send_message.assert_awaited_once_with(
        agent_id="agent-1",
        session_id="session-1",
        content="hello",
    )
