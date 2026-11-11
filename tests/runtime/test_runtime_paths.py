from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from witty_service.runtime.paths import (
    WorkspaceRuntimePaths,
    resolve_workspace_path,
    workspace_relative_path,
)


def test_runtime_home_layout(tmp_path: Path) -> None:
    paths = WorkspaceRuntimePaths.for_workspace("ws_a", root=tmp_path)

    assert paths.runtime_home == tmp_path / "runtime" / "ws_a"
    assert paths.config == paths.runtime_home / "config"
    assert paths.data == paths.runtime_home / "data"
    assert paths.state == paths.runtime_home / "state"
    assert paths.cache == paths.runtime_home / "cache"

    paths.ensure()

    for path in (
        paths.runtime_home,
        paths.config,
        paths.data,
        paths.state,
        paths.cache,
    ):
        assert path.is_dir()
        assert path.stat().st_mode & 0o777 == 0o700


def test_environment_isolated_from_passwd_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCODE_CONFIG", "/root/.config/opencode/opencode.json")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", "{}")
    monkeypatch.setenv("OPENCODE_DB", "/root/.local/share/opencode/db")
    monkeypatch.setenv("WITTY_API_TOKEN", "secret")
    paths = WorkspaceRuntimePaths.for_workspace("ws_a", root=tmp_path)

    env = paths.environment()

    assert env["HOME"] == str(paths.runtime_home)
    assert env["XDG_CONFIG_HOME"] == str(paths.config)
    assert env["XDG_DATA_HOME"] == str(paths.data)
    assert env["XDG_STATE_HOME"] == str(paths.state)
    assert env["XDG_CACHE_HOME"] == str(paths.cache)
    assert not any(key.startswith("OPENCODE_") for key in env)
    assert "WITTY_API_TOKEN" not in env
    assert env["PATH"] == os.environ["PATH"]


@pytest.mark.parametrize("workspace_id", ["", ".", "..", "a/b", "a\\b", "nul\x00"])
def test_rejects_invalid_workspace_id(tmp_path: Path, workspace_id: str) -> None:
    with pytest.raises(ValueError):
        WorkspaceRuntimePaths.for_workspace(workspace_id, root=tmp_path)


def test_ensure_rejects_symlinked_runtime_home(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / "ws_a").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        WorkspaceRuntimePaths.for_workspace("ws_a", root=tmp_path).ensure()


@pytest.mark.parametrize(
    "relative_path",
    ["../escape", "/etc/passwd", "C:\\windows\\system32", "a\\b", "", ".", ".."],
)
def test_rejects_escaping_relative_paths(tmp_path: Path, relative_path: str) -> None:
    with pytest.raises(ValueError):
        resolve_workspace_path(tmp_path, relative_path)


def test_rejects_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes workspace"):
        resolve_workspace_path(workspace, "link/file.txt")


def test_resolves_nested_workspace_path(tmp_path: Path) -> None:
    target = resolve_workspace_path(tmp_path, "output/report.md")

    assert target == tmp_path / "output" / "report.md"


def test_workspace_relative_path_normalizes(tmp_path: Path) -> None:
    artifact = tmp_path / "output" / "report.md"
    artifact.parent.mkdir()
    artifact.write_text("x")

    assert workspace_relative_path(tmp_path, artifact) == "output/report.md"
    assert workspace_relative_path(tmp_path, "output/report.md") == "output/report.md"


def test_workspace_relative_path_rejects_outside(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside workspace"):
        workspace_relative_path(tmp_path / "workspace", tmp_path / "other.txt")


def test_runtime_paths_do_not_depend_on_legacy_workspace_paths() -> None:
    module = Path(__file__).resolve().parents[2] / "src" / "witty_service" / "runtime"
    for path in module.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "witty_service.workspace_paths"
            ):
                pytest.fail(f"{path.name} imports legacy workspace_paths")
