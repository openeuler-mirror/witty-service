"""
E2E tests for Session Proxy flow.
These tests verify the complete session proxy flow through the API.
"""
import pytest
from httpx import AsyncClient, ASGITransport
from src.main import create_app

AUTH_HEADERS = {"Authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_session_proxy_flow():
    """Test the complete session proxy flow: create agent, create session, list sessions, delete session, cleanup."""
    app = create_app()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Create agent
        response = await client.post(
            "/api/v1/agents",
            json={
                "name": "test-agent",
                "sandbox_type": "local_process",
                "adapter_type": "openclaw",
                "idle_timeout_seconds": 3600,
            },
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 201
        agent_id = response.json()["id"]

    # 2. Create session (should proxy to witty-agent-server)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/agents/{agent_id}/sessions",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 201
        session_id = response.json()["id"]
        assert "context_initialized" in response.json()

    # 3. List sessions (should refresh local cache)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/v1/agents/{agent_id}/sessions",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200
        assert len(response.json()) >= 1

    # 4. Delete session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(
            f"/api/v1/agents/{agent_id}/sessions/{session_id}",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 204

    # 5. Cleanup - delete agent
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(
            f"/api/v1/agents/{agent_id}",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 204