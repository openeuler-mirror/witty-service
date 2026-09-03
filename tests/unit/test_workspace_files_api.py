from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from witty_service.api import agents as agents_api
from witty_service.domain.enums import AgentStatus
from witty_service.persistence.repositories import AgentRecord


def _make_agent(workspace_path: str) -> AgentRecord:
    now = datetime.now(UTC)
    return AgentRecord(
        id="agent-1",
        name="demo",
        description="",
        sandbox_type="local_process",
        adapter_type="http",
        status=AgentStatus.running,
        sandbox_id="sandbox-1",
        workspace_path=workspace_path,
        idle_timeout_seconds=300,
        model_id=None,
        mcp_server_list=[],
        last_active_at=None,
        created_at=now,
        updated_at=now,
    )


def _handler_services(agent: AgentRecord) -> MagicMock:
    manager = MagicMock()
    manager._check_and_update_agent_status_if_needed.return_value = agent
    services = MagicMock()
    services.get_agent_manager_for_agent.return_value = manager
    return services


def _make_file(tmp_path: Path) -> Path:
    output = tmp_path / "output"
    output.mkdir()
    target = output / "demo.html"
    target.write_text("<h1>hello</h1>", encoding="utf-8")
    return target


def test_workspace_file_served_inline_and_download(tmp_path: Path) -> None:
    target = _make_file(tmp_path)

    inline = agents_api.get_workspace_file(
        agent_id="agent-1",
        path="output/demo.html",
        download=0,
        services=_handler_services(_make_agent(str(tmp_path))),
    )
    assert inline.status_code == 200
    assert Path(inline.path) == target
    assert inline.media_type == "text/html"
    assert inline.headers.get("content-disposition") is None

    download = agents_api.get_workspace_file(
        agent_id="agent-1",
        path="output/demo.html",
        download=1,
        services=_handler_services(_make_agent(str(tmp_path))),
    )
    assert "attachment" in download.headers["content-disposition"]
    assert "demo.html" in download.headers["content-disposition"]


@pytest.mark.parametrize(
    "path",
    [
        "../secret.txt",
        "output/../../secret.txt",
        "/etc/passwd",
        "..",
    ],
)
def test_workspace_file_rejects_traversal(tmp_path: Path, path: str) -> None:
    with pytest.raises(HTTPException) as exc:
        agents_api.get_workspace_file(
            agent_id="agent-1",
            path=path,
            services=_handler_services(_make_agent(str(tmp_path))),
        )
    assert exc.value.status_code == 400


def test_workspace_file_rejects_symlink_escape(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    outside_dir = Path(tempfile.mkdtemp())
    outside = outside_dir / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (output / "link.html").symlink_to(outside)

    with pytest.raises(HTTPException) as exc:
        agents_api.get_workspace_file(
            agent_id="agent-1",
            path="output/link.html",
            services=_handler_services(_make_agent(str(tmp_path))),
        )
    assert exc.value.status_code == 403


def test_workspace_file_missing_returns_404(tmp_path: Path) -> None:
    with pytest.raises(HTTPException) as exc:
        agents_api.get_workspace_file(
            agent_id="agent-1",
            path="output/nope.html",
            services=_handler_services(_make_agent(str(tmp_path))),
        )
    assert exc.value.status_code == 404
