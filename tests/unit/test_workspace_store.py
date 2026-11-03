from __future__ import annotations

import pytest

from witty_service.storage.workspace_store import LocalWorkspaceStore, WorkspaceStore


def test_workspace_store_requires_a_base_path() -> None:
    with pytest.raises(TypeError, match="requires base_dir or base_path"):
        WorkspaceStore()


def test_workspace_store_rejects_conflicting_base_paths(tmp_path) -> None:
    with pytest.raises(ValueError, match="must refer to the same path"):
        WorkspaceStore(base_dir=tmp_path / "a", base_path=tmp_path / "b")


def test_workspace_store_initializes_and_cleans_agent_workspace(tmp_path) -> None:
    store = WorkspaceStore(base_dir=tmp_path)

    workspace = store.init_workspace("agent-1")

    assert workspace == tmp_path / "agent-workspaces" / "agent-1" / "workspace"
    assert (workspace / ".agent").is_dir()
    assert (workspace / "code").is_dir()
    assert (workspace / "input").is_dir()
    assert (workspace / "output").is_dir()

    store.cleanup_workspace("agent-1")

    assert not workspace.exists()


@pytest.mark.parametrize("agent_id", ["", ".", "..", "../evil", "a/b", "a\\b"])
def test_workspace_store_rejects_invalid_agent_ids(tmp_path, agent_id) -> None:
    store = WorkspaceStore(base_dir=tmp_path)

    with pytest.raises(ValueError):
        store.init_workspace(agent_id)


def test_local_workspace_store_uses_default_base_path(tmp_path) -> None:
    store = LocalWorkspaceStore(base_path=tmp_path)

    assert store.base_dir == tmp_path.resolve()


def test_cleanup_is_idempotent_when_directories_are_missing(tmp_path) -> None:
    store = WorkspaceStore(base_dir=tmp_path)

    store.cleanup_workspace("agent-1")
    store.cleanup_runtime_instances("agent-1")

    assert not (tmp_path / "agent-workspaces").exists()


def test_cleanup_does_not_touch_retired_runtime_directories(tmp_path) -> None:
    """旧 Runtime 目录（dsh-instances）由其他分支管理，本分支删除时必须保留。"""
    dsh_dir = tmp_path / "dsh-instances" / "agent-1"
    dsh_dir.mkdir(parents=True)

    store = WorkspaceStore(base_dir=tmp_path)
    store.init_workspace("agent-1")
    store.cleanup_workspace("agent-1")
    store.cleanup_runtime_instances("agent-1")

    assert dsh_dir.is_dir()


def test_cleanup_refuses_symlink_escaping_the_workspace_root(tmp_path) -> None:
    """agent 目录被替换成指向根目录外的软链时必须拒绝删除。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("important", encoding="utf-8")

    base = tmp_path / "base"
    agent_wrapper = base / "agent-workspaces" / "agent-1"
    agent_wrapper.parent.mkdir(parents=True)
    agent_wrapper.symlink_to(outside, target_is_directory=True)

    store = WorkspaceStore(base_dir=base)
    with pytest.raises(ValueError, match="outside managed directory"):
        store.cleanup_workspace("agent-1")

    assert (outside / "keep.txt").exists()


def test_cleanup_removes_symlink_without_following_it(tmp_path) -> None:
    """目录被替换成软链时只删链接本身（rmtree 对软链会抛 OSError）。"""
    target = tmp_path / "agent-workspaces" / "shared-target"
    target.mkdir(parents=True)
    (target / "keep.txt").write_text("keep", encoding="utf-8")

    agent_wrapper = tmp_path / "agent-workspaces" / "agent-1"
    agent_wrapper.symlink_to(target, target_is_directory=True)

    instance_target = tmp_path / "opencode-instances" / "shared-target"
    instance_target.mkdir(parents=True)
    instance_link = tmp_path / "opencode-instances" / "agent-1"
    instance_link.symlink_to(instance_target, target_is_directory=True)

    store = WorkspaceStore(base_dir=tmp_path)
    store.cleanup_workspace("agent-1")
    store.cleanup_runtime_instances("agent-1")

    assert not agent_wrapper.is_symlink()
    assert not instance_link.is_symlink()
    assert (target / "keep.txt").exists()
    assert instance_target.is_dir()


def test_cleanup_rejects_invalid_agent_ids(tmp_path) -> None:
    store = WorkspaceStore(base_dir=tmp_path)

    with pytest.raises(ValueError):
        store.cleanup_workspace("../evil")
    with pytest.raises(ValueError):
        store.cleanup_runtime_instances("../evil")
