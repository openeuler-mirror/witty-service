from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from witty_service import workspace_paths as resolver_mod
from witty_service.storage.workspace_store import WorkspaceStore
from witty_service.workspace_paths import agent_workspace_path


def test_agent_workspace_path_uses_root_param(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    expected = (root / "agent-workspaces" / "agent-1" / "workspace").resolve()
    assert agent_workspace_path("agent-1", root=root) == expected


def test_agent_workspace_path_defaults_to_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "ws"
    mock_settings = SimpleNamespace(
        workspace=SimpleNamespace(root_path=lambda: root)
    )
    monkeypatch.setattr(resolver_mod, "get_settings", lambda: mock_settings)
    expected = (root / "agent-workspaces" / "agent-1" / "workspace").resolve()
    assert agent_workspace_path("agent-1") == expected


@pytest.mark.parametrize(
    "bad_agent_id",
    ["", ".", "..", "agent/../x", "../x", "/abs", "a/b"],
)
def test_agent_workspace_path_rejects_invalid_agent_id(bad_agent_id: str) -> None:
    with pytest.raises(ValueError):
        agent_workspace_path(bad_agent_id)


def test_agent_workspace_path_rejects_backslash() -> None:
    with pytest.raises(ValueError):
        agent_workspace_path("agent" + chr(92) + "x")


def test_store_uses_same_derivation_as_shared(tmp_path: Path) -> None:
    """跨层一致性：存储层创建路径必须与共享推导函数完全一致（单点保证）。"""
    store = WorkspaceStore(base_dir=tmp_path)
    assert store._agent_workspace_path("agent-1") == agent_workspace_path(
        "agent-1", root=store.base_dir
    )
    assert store.init_workspace("agent-1") == (
        tmp_path / "agent-workspaces" / "agent-1" / "workspace"
    ).resolve()
