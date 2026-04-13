"""
E2E tests for Agent Lifecycle operations.
These tests verify pause/resume and delete/resume flows.
"""
import pytest
from httpx import AsyncClient, ASGITransport
from src.main import create_app

AUTH_HEADERS = {"Authorization": "Bearer test-token"}


@pytest.mark.asyncio
async def test_pause_resume_flow():
    """Test pause and resume agent lifecycle operations."""
    app = create_app()

    # 1. Create agent
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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

    # 2. Pause
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/agents/{agent_id}/pause",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "paused"

    # 3. Resume
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/agents/{agent_id}/resume",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "running"

    # Cleanup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.delete(
            f"/api/v1/agents/{agent_id}",
            headers=AUTH_HEADERS,
        )


@pytest.mark.asyncio
async def test_delete_resume_flow():
    """Test resume from deleted agent - agent should be recreated."""
    app = create_app()

    # 1. Create agent
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
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

    # 2. Delete
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.delete(
            f"/api/v1/agents/{agent_id}",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 204

    # 3. Resume from deleted - should recreate agent and return running status
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/agents/{agent_id}/resume",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "running"