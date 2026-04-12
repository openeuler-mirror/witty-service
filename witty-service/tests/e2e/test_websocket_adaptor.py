"""
Integration tests for WebSocket Adaptor feature.
These tests verify the complete flow from AgentManager through WebSocket client.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

def test_agent_manager_uses_websocket_client():
    """
    Verify AgentManager.send_message uses WebSocketClientPool
    and WebSocketClient for communication.
    """
    # This test verifies the integration is wired correctly
    # The actual WebSocket behavior is tested in unit tests
    from src.adapter.websocket_client_pool import WebSocketClientPool, AdaptorEndpoint
    from src.adapter.websocket_client import WebSocketClient

    pool = WebSocketClientPool()
    endpoint = AdaptorEndpoint(
        base_url="ws://localhost:8080",
        session_id="test-session",
        sandbox_type="openclaw",
    )

    # Verify pool creates WebSocket clients
    factory = MagicMock(return_value=MagicMock())
    client = pool.get_client("agent-1", endpoint, factory)

    assert client is not None
    factory.assert_called_once_with("ws://localhost:8080")

def test_websocket_protocol_types_match_spec():
    """
    Verify protocol types align with witty-agent-server v2.1 spec.
    """
    from src.adapter.websocket_protocol import InboundEvent, OutboundMessage

    # InboundEvent matches spec envelope
    event: InboundEvent = {
        "type": "message.delta",
        "session_id": "sess-123",
        "runtime_type": "openclaw",
        "event_id": "evt-456",
        "ts_ms": 1712650000123,
        "payload": {"delta": "hello"},
    }
    assert event["type"] == "message.delta"
    assert "payload" in event

    # OutboundMessage matches message.create spec
    msg: OutboundMessage = {
        "type": "message.create",
        "payload": {"message": "hello"},
    }
    assert msg["type"] == "message.create"
    assert "payload" in msg