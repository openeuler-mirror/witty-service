from __future__ import annotations

import json
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from witty_service.domain.errors import DomainError, SessionBusyError
from witty_service.main import create_app


def _parse_sse_payloads(body: bytes) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for line in body.decode("utf-8").splitlines():
        if not line:
            continue
        assert line.startswith("data: ")
        payloads.append(json.loads(line.removeprefix("data: ")))
    return payloads


def test_send_message_stream_returns_sse_events(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "test-token")

    async def event_stream():
        yield {
            "sandbox_type": "local_process",
            "event": {
                "type": "message.delta",
                "session_id": "session-1",
                "runtime_type": "openclaw",
                "event_id": "evt-1",
                "ts_ms": 123,
                "payload": {"delta": "hello"},
            },
        }
        yield {
            "sandbox_type": "local_process",
            "event": {
                "type": "message.completed",
                "session_id": "session-1",
                "runtime_type": "openclaw",
                "event_id": "evt-2",
                "ts_ms": 456,
                "payload": {},
            },
        }

    manager = MagicMock()
    manager.send_message_stream.return_value = event_stream()

    services = MagicMock()
    services.get_agent_manager_for_agent.return_value = manager

    client = TestClient(create_app(services=services))

    with client.stream(
        "POST",
        "/agents/agent-1/sessions/session-1/messages/stream",
        headers={"Authorization": "Bearer test-token"},
        json={"content": "hello"},
    ) as resp:
        body = b"".join(resp.iter_bytes())

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert _parse_sse_payloads(body) == [
        {
            "sandbox_type": "local_process",
            "event": {
                "type": "message.delta",
                "session_id": "session-1",
                "runtime_type": "openclaw",
                "event_id": "evt-1",
                "ts_ms": 123,
                "payload": {"delta": "hello"},
            },
        },
        {
            "sandbox_type": "local_process",
            "event": {
                "type": "message.completed",
                "session_id": "session-1",
                "runtime_type": "openclaw",
                "event_id": "evt-2",
                "ts_ms": 456,
                "payload": {},
            },
        },
    ]
    manager.send_message_stream.assert_called_once_with(
        agent_id="agent-1",
        session_id="session-1",
        content="hello",
    )


def test_send_message_stream_returns_standard_error_before_first_event(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "test-token")

    async def failing_stream():
        raise DomainError(
            code="AGENT_NOT_RUNNING",
            message="Agent must be running to send messages.",
            details={"agent_id": "agent-1", "status": "creating"},
        )
        yield {}

    manager = MagicMock()
    manager.send_message_stream.return_value = failing_stream()

    services = MagicMock()
    services.get_agent_manager_for_agent.return_value = manager

    client = TestClient(create_app(services=services))

    resp = client.post(
        "/agents/agent-1/sessions/session-1/messages/stream",
        headers={"Authorization": "Bearer test-token"},
        json={"content": "hello"},
    )

    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == {
        "error": {
            "code": "AGENT_NOT_RUNNING",
            "message": "Agent must be running to send messages.",
            "details": {"agent_id": "agent-1", "status": "creating"},
        }
    }


def test_send_message_stream_reuses_send_message_request_validation(monkeypatch):
    monkeypatch.setenv("AUTH_TOKEN", "test-token")

    services = MagicMock()
    client = TestClient(create_app(services=services))

    resp = client.post(
        "/agents/agent-1/sessions/session-1/messages/stream",
        headers={"Authorization": "Bearer test-token"},
        json={"content": ""},
    )

    assert resp.status_code == 422
    services.get_agent_manager_for_agent.assert_not_called()


def test_regenerate_message_stream_returns_sse_events(monkeypatch):
    """重生成端点复用同一 SSE 事件契约，且不需要请求体。"""
    monkeypatch.setenv("AUTH_TOKEN", "test-token")

    async def event_stream():
        yield {
            "sandbox_type": "local_process",
            "event": {
                "type": "message.delta",
                "session_id": "session-1",
                "runtime_type": "openclaw",
                "event_id": "evt-1",
                "ts_ms": 123,
                "payload": {"delta": "重生成中"},
            },
        }
        yield {
            "sandbox_type": "local_process",
            "event": {
                "type": "message.completed",
                "session_id": "session-1",
                "runtime_type": "openclaw",
                "event_id": "evt-2",
                "ts_ms": 456,
                "payload": {"text": "重生成结果"},
            },
        }

    manager = MagicMock()
    manager.regenerate_message_stream.return_value = event_stream()

    services = MagicMock()
    services.get_agent_manager_for_agent.return_value = manager

    client = TestClient(create_app(services=services))

    with client.stream(
        "POST",
        "/agents/agent-1/sessions/session-1/messages/regenerate",
        headers={"Authorization": "Bearer test-token"},
    ) as resp:
        body = b"".join(resp.iter_bytes())

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    payloads = _parse_sse_payloads(body)
    assert [payload["event"]["type"] for payload in payloads] == [
        "message.delta",
        "message.completed",
    ]
    manager.regenerate_message_stream.assert_called_once_with(
        agent_id="agent-1",
        session_id="session-1",
    )


def test_regenerate_message_stream_reports_session_busy_as_409(monkeypatch):
    """同会话已有生成在飞：首个事件前抛出即转成标准 JSON 错误（409）。"""
    monkeypatch.setenv("AUTH_TOKEN", "test-token")

    async def failing_stream():
        raise SessionBusyError(session_id="session-1")
        yield {}

    manager = MagicMock()
    manager.regenerate_message_stream.return_value = failing_stream()

    services = MagicMock()
    services.get_agent_manager_for_agent.return_value = manager

    client = TestClient(create_app(services=services))

    resp = client.post(
        "/agents/agent-1/sessions/session-1/messages/regenerate",
        headers={"Authorization": "Bearer test-token"},
    )

    assert resp.status_code == 409
    assert resp.json() == {
        "error": {
            "code": "SESSION_BUSY",
            "message": "Session already has a generation in flight.",
            "details": {"session_id": "session-1"},
        }
    }
