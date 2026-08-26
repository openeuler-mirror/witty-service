from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from witty_service.api import agents as agents_api
from witty_service.domain.enums import AgentStatus
from witty_service.persistence.repositories import AgentRecord, McpServerRecord


def test_enable_cvekit_mcp_injects_cve_and_model_credentials_at_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeAdaptorClient:
        def __init__(self, base_url: str) -> None:
            captured["base_url"] = base_url

        async def post(self, path: str, json: dict[str, object]) -> None:
            captured["path"] = path
            captured["payload"] = json

        async def close(self) -> None:
            return None

    monkeypatch.setattr(agents_api, "AdaptorHttpClient", FakeAdaptorClient)
    now = datetime.now(UTC)
    agent = AgentRecord(
        id="agent-1",
        name="CVE",
        description="",
        sandbox_type="local_process",
        adapter_type="http",
        status=AgentStatus.running,
        sandbox_id="sandbox-1",
        workspace_path=str(tmp_path),
        idle_timeout_seconds=300,
        model_id="model-1",
        mcp_server_list=[],
        last_active_at=None,
        created_at=now,
        updated_at=now,
    )
    mcp_server = McpServerRecord(
        id="mcp-1",
        mcp_server_name="cvekit_mcp",
        mcp_server_config={"command": "cvekit", "env": {"CVEKIT_LOG_DIR": "/tmp/logs"}},
        created_at=now,
        updated_at=now,
    )
    services = MagicMock()
    services.workspace_store.base_dir = tmp_path / "workspace"
    config_path = services.workspace_store.base_dir / "config" / "cve.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"gitcode_token": "saved-test-token"}', encoding="utf-8")
    services.repository.get_agent.return_value = agent
    services.repository.get_mcp_server.return_value = mcp_server
    services.repository.get_model.return_value = SimpleNamespace(
        api_key="model-test-key",
        provider="deepseek",
        api_base_url="https://api.example.test/v1",
        name="deepseek-v4-flash",
    )
    services.repository.get_sandbox_state.return_value = SimpleNamespace(
        adapter_base_url="http://adapter"
    )

    asyncio.run(agents_api.enable_mcp_server("agent-1", "mcp-1", services))

    assert captured["path"] == "/agent/mcp/enable"
    assert captured["payload"] == {
        "mcp_server_name": "cvekit_mcp",
        "mcp_server_config": {
            "command": "cvekit",
            "env": {
                "CVEKIT_LOG_DIR": "/tmp/logs",
                "GITCODE_TOKEN": "saved-test-token",
                "API_KEY": "model-test-key",
                "LLM_PROVIDER": "deepseek",
                "LLM_BASE_URL": "https://api.example.test/v1",
                "LLM_MODEL_NAME": "deepseek-v4-flash",
            },
        },
    }
    assert mcp_server.mcp_server_config["env"] == {"CVEKIT_LOG_DIR": "/tmp/logs"}
