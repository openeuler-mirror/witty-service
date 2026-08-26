from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

from witty_service.application.cve_service import CveService


def _service(tmp_path: Path) -> CveService:
    services = Mock()
    services.workspace_store.base_dir = tmp_path / "workspace"
    return CveService(services)


def test_update_token_writes_only_cve_config(tmp_path: Path) -> None:
    service = _service(tmp_path)

    service.update_token(" test-gitcode-token ")

    cve_config = json.loads(service._config_path.read_text(encoding="utf-8"))
    assert cve_config["gitcode_token"] == "test-gitcode-token"
    service._services.repository.update_mcp_server.assert_not_called()


def test_update_token_does_not_require_cvekit_mcp(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service._services.repository.list_mcp_servers.return_value = []

    service.update_token("new-token")

    assert service.get_config()["gitcode_token"] == "new-token"
    service._services.repository.update_mcp_server.assert_not_called()
