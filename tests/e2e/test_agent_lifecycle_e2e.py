"""
E2E tests for Agent Lifecycle operations.
These tests verify pause/resume and delete/resume flows.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from witty_service.main import create_app

AUTH_HEADERS = {"Authorization": "Bearer test-token"}


@pytest.mark.skip(
    reason="需要真实 LLM provider 与 witty-agent-server runtime 配置，当前环境无法完成 /agent/start，先跳过"
)
@pytest.mark.asyncio
async def test_pause_resume_flow(monkeypatch):
    """Test pause and resume agent lifecycle operations."""
    _use_test_token(monkeypatch)
    app = create_app()

    # 1. Create agent
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/agents",
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
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/agents/{agent_id}/pause",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "paused"

    # 3. Resume
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/agents/{agent_id}/resume",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "running"

    # Cleanup
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.delete(
            f"/agents/{agent_id}",
            headers=AUTH_HEADERS,
        )


@pytest.mark.skip(
    reason="需要真实 LLM provider 与 witty-agent-server runtime 配置，当前环境无法完成 /agent/start，先跳过"
)
@pytest.mark.asyncio
async def test_delete_resume_flow(monkeypatch):
    """Test resume from deleted agent - agent should be recreated."""
    _use_test_token(monkeypatch)
    app = create_app()

    # 1. Create agent
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/agents",
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
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.delete(
            f"/agents/{agent_id}",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 204

    # 3. Resume from deleted - should recreate agent and return running status
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/agents/{agent_id}/resume",
            headers=AUTH_HEADERS,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "running"


def _use_test_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """让本测试使用固定 token 并重置全局 settings 单例。"""
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    monkeypatch.setattr("witty_service.config._settings", None)
