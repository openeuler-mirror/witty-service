from typing import Any
from src.adapter.websocket_protocol import InboundEvent, OutboundMessage

def test_inbound_event_structure():
    event: InboundEvent = {
        "type": "message.delta",
        "session_id": "sess-123",
        "runtime_type": "openclaw",
        "event_id": "evt-456",
        "ts_ms": 1712650000123,
        "payload": {"delta": "hello"},
    }
    assert event["type"] == "message.delta"
    assert event["session_id"] == "sess-123"

def test_outbound_message_structure():
    msg: OutboundMessage = {
        "type": "message.create",
        "payload": {"message": "hello"},
    }
    assert msg["type"] == "message.create"
    assert msg["payload"]["message"] == "hello"