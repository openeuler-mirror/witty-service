import pytest
from unittest.mock import AsyncMock, MagicMock
from src.adapter.websocket_client_pool import WebSocketClientPool, AdaptorEndpoint
from src.adapter.websocket_client import WebSocketClient

def test_get_client_creates_new_client():
    pool = WebSocketClientPool()
    endpoint = AdaptorEndpoint(
        base_url="ws://localhost:8080",
        session_id="session-1",
        sandbox_type="openclaw",
    )
    factory = MagicMock(return_value=MagicMock())

    client = pool.get_client("agent-1", endpoint, factory)

    factory.assert_called_once_with("ws://localhost:8080")
    assert "agent-1" in pool._clients

def test_get_client_returns_same_client_for_same_agent():
    pool = WebSocketClientPool()
    endpoint = AdaptorEndpoint(
        base_url="ws://localhost:8080",
        session_id="session-1",
        sandbox_type="openclaw",
    )
    mock_client = MagicMock()
    factory = MagicMock(return_value=mock_client)

    client1 = pool.get_client("agent-1", endpoint, factory)
    client2 = pool.get_client("agent-1", endpoint, factory)

    assert client1 is client2
    assert factory.call_count == 1

def test_remove_client():
    pool = WebSocketClientPool()
    endpoint = AdaptorEndpoint(
        base_url="ws://localhost:8080",
        session_id="session-1",
        sandbox_type="openclaw",
    )
    mock_client = AsyncMock()
    factory = MagicMock(return_value=mock_client)

    pool.get_client("agent-1", endpoint, factory)
    pool.remove_client("agent-1")

    assert "agent-1" not in pool._clients

@pytest.mark.asyncio
async def test_close_all_closes_all_clients():
    pool = WebSocketClientPool()
    endpoint = AdaptorEndpoint(
        base_url="ws://localhost:8080",
        session_id="session-1",
        sandbox_type="openclaw",
    )
    mock_client1 = AsyncMock()
    mock_client2 = AsyncMock()

    # Create two clients
    factory = MagicMock(side_effect=[mock_client1, mock_client2])
    pool.get_client("agent-1", endpoint, factory)
    pool.get_client("agent-2", endpoint, factory)

    # Close all
    await pool.close_all()

    # Verify close was called on both
    mock_client1.close.assert_called_once()
    mock_client2.close.assert_called_once()

    # Verify clients dict is cleared
    assert len(pool._clients) == 0