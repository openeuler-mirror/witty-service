import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from src.adapter.websocket_client import WebSocketClient
from src.adapter.websocket_protocol import InboundEvent, OutboundMessage
from src.adapter.exceptions import AdaptorConnectionError, AdaptorReceiveError, AdaptorSendFailed

def test_client_connect_success():
    async def run() -> None:
        client = WebSocketClient(base_url="ws://localhost:8080")
        with patch("websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_ws = AsyncMock()
            mock_connect.return_value = mock_ws

            await client.connect("session-1")

            mock_connect.assert_called_once_with(
                "ws://localhost:8080/agent/sessions/session-1/ws"
            )
            assert client.is_connected is True

    asyncio.run(run())

def test_client_send_message():
    async def run() -> None:
        client = WebSocketClient(base_url="ws://localhost:8080")
        mock_ws = AsyncMock()
        client._ws = mock_ws
        client._connected = True

        msg: OutboundMessage = {"type": "message.create", "payload": {"message": "hello"}}
        await client.send(msg)

        mock_ws.send.assert_called_once()
        sent_data = mock_ws.send.call_args[0][0]
        import json
        assert json.loads(sent_data) == msg

    asyncio.run(run())

def test_client_recv_yields_events():
    async def run() -> None:
        client = WebSocketClient(base_url="ws://localhost:8080")
        mock_ws = AsyncMock()
        mock_ws.__aiter__ = lambda self: self
        mock_ws.__anext__ = AsyncMock(side_effect=[
            '{"type":"message.delta","session_id":"s1","runtime_type":"openclaw","event_id":"e1","ts_ms":123,"payload":{"delta":"hi"}}',
            StopAsyncIteration()
        ])
        client._ws = mock_ws
        client._connected = True

        events = []
        async for event in client.recv():
            events.append(event)

        assert len(events) == 1
        assert events[0]["type"] == "message.delta"
        assert events[0]["runtime_type"] == "openclaw"
        assert "sandbox_type" not in events[0]

    asyncio.run(run())


def test_client_recv_requires_runtime_type():
    async def run() -> None:
        class MissingRuntimeTypeStream:
            def __init__(self) -> None:
                self._items = [
                    '{"type":"message.delta","session_id":"s1","event_id":"e1","ts_ms":123,"payload":{"delta":"hi"}}'
                ]

            def __aiter__(self) -> "MissingRuntimeTypeStream":
                return self

            async def __anext__(self) -> str:
                if not self._items:
                    raise StopAsyncIteration
                return self._items.pop(0)

        client = WebSocketClient(base_url="ws://localhost:8080")
        client._ws = MissingRuntimeTypeStream()
        client._connected = True

        with pytest.raises(AdaptorReceiveError) as exc_info:
            async for _ in client.recv():
                pass

        assert exc_info.value.message == "Failed to parse WebSocket message"
        assert "\"runtime_type\"" not in exc_info.value.details["raw"]

    asyncio.run(run())
