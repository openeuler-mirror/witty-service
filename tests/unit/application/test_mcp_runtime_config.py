from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from witty_service.application.mcp_runtime_config import McpRuntimeConfigResolver


def test_resolve_cvekit_runtime_config_injects_cve_token_without_mutation(
    tmp_path: Path,
) -> None:
    services = MagicMock()
    services.workspace_store.base_dir = tmp_path / "workspace"
    config_path = services.workspace_store.base_dir / "config" / "cve.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps({"gitcode_token": "saved-test-token"}), encoding="utf-8"
    )
    stored_config = {
        "cvekit_mcp": {
            "command": "cvekit",
            "env": {"CVEKIT_LOG_DIR": "/tmp/logs", "GITEE_TOKEN": "legacy"},
        }
    }

    runtime_config = McpRuntimeConfigResolver(services).resolve(
        "cvekit_mcp", stored_config
    )

    assert runtime_config["cvekit_mcp"]["env"] == {
        "CVEKIT_LOG_DIR": "/tmp/logs",
        "GITCODE_TOKEN": "saved-test-token",
    }
    assert stored_config["cvekit_mcp"]["env"] == {
        "CVEKIT_LOG_DIR": "/tmp/logs",
        "GITEE_TOKEN": "legacy",
    }


def test_resolve_non_cvekit_runtime_config_returns_an_independent_copy() -> None:
    stored_config = {"filesystem": {"command": "npx"}}

    runtime_config = McpRuntimeConfigResolver(MagicMock()).resolve(
        "filesystem", stored_config
    )

    assert runtime_config == stored_config
    assert runtime_config is not stored_config
