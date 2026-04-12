from pathlib import Path

import pytest

from witty_service.storage.workspace_store import LocalWorkspaceStore, WorkspaceStore


def test_init_workspace_creates_required_dirs(tmp_path: Path):
    store = WorkspaceStore(base_dir=tmp_path)

    workspace_path = store.init_workspace("agent-1")

    assert workspace_path == tmp_path / "agent-1" / "workspace"
    assert (workspace_path / ".agent").is_dir()
    assert (workspace_path / "code").is_dir()
    assert (workspace_path / "input").is_dir()
    assert (workspace_path / "output").is_dir()


def test_cleanup_workspace_removes_agent_directory(tmp_path: Path):
    store = WorkspaceStore(base_dir=tmp_path)
    workspace_path = store.init_workspace("agent-1")
    (workspace_path / ".agent" / "state.json").write_text("{}", encoding="utf-8")

    store.cleanup_workspace("agent-1")

    assert not (tmp_path / "agent-1" / "workspace").exists()
    assert (tmp_path / "agent-1").exists()


def test_init_workspace_is_idempotent(tmp_path: Path):
    store = WorkspaceStore(base_dir=tmp_path)

    first_path = store.init_workspace("agent-1")
    second_path = store.init_workspace("agent-1")

    assert first_path == second_path
    assert (second_path / ".agent").is_dir()
    assert (second_path / "code").is_dir()
    assert (second_path / "input").is_dir()
    assert (second_path / "output").is_dir()


@pytest.mark.parametrize("agent_id", ["", ".", "..", "../x", "x/y", "x\\y"])
def test_init_workspace_rejects_invalid_agent_id(agent_id: str, tmp_path: Path):
    store = WorkspaceStore(base_dir=tmp_path)

    with pytest.raises(ValueError):
        store.init_workspace(agent_id)


@pytest.mark.parametrize("agent_id", ["", ".", "..", "../x", "x/y", "x\\y"])
def test_cleanup_workspace_rejects_invalid_agent_id(agent_id: str, tmp_path: Path):
    store = WorkspaceStore(base_dir=tmp_path)

    with pytest.raises(ValueError):
        store.cleanup_workspace(agent_id)


def test_cleanup_workspace_does_not_remove_base_dir(tmp_path: Path):
    store = WorkspaceStore(base_dir=tmp_path)
    outside_dir = tmp_path.parent / "outside"
    outside_dir.mkdir(exist_ok=True)

    with pytest.raises(ValueError):
        store.cleanup_workspace("")

    assert tmp_path.exists()
    assert outside_dir.exists()


def test_local_workspace_store_accepts_legacy_base_path(tmp_path: Path):
    store = LocalWorkspaceStore(base_path=tmp_path)

    workspace_path = store.init_workspace("agent-legacy")

    assert workspace_path == tmp_path / "agent-legacy" / "workspace"


def test_local_workspace_store_defaults_to_service_path():
    store = LocalWorkspaceStore()

    assert store.base_dir == Path("/data/agent-workspaces")
