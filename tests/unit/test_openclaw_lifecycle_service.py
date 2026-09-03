from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from witty_agent_server.application.services.agent.openclaw_lifecycle_service import (
    OpenClawLifecycleError,
    OpenClawLifecycleService,
)
from witty_service import workspace_paths as resolver_mod


def _patch_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """让 resolver 模块的 get_settings() 返回以 root 为 workspace 根的桩配置。"""
    monkeypatch.setattr(
        resolver_mod,
        "get_settings",
        lambda: SimpleNamespace(
            workspace=SimpleNamespace(root_path=lambda: root)
        ),
    )


def test_workspace_path_derives_from_profile(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "ws"
    _patch_root(monkeypatch, root)
    svc = OpenClawLifecycleService(profile="agent-99")
    assert svc._workspace_path() == (
        root / "agent-workspaces" / "agent-99" / "workspace"
    ).resolve()


def test_workspace_path_none_without_profile(tmp_path: Path, monkeypatch) -> None:
    _patch_root(monkeypatch, tmp_path / "ws")
    svc = OpenClawLifecycleService()
    assert svc._workspace_path() is None


def test_openclaw_env_sets_workspace_dir(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "ws"
    _patch_root(monkeypatch, root)
    monkeypatch.setenv("MY_VAR", "x")
    svc = OpenClawLifecycleService(profile="agent-1")
    env = svc._openclaw_env()
    assert env is not None
    assert env["OPENCLAW_WORKSPACE_DIR"] == str(
        (root / "agent-workspaces" / "agent-1" / "workspace").resolve()
    )
    # 保留其余环境变量
    assert env["MY_VAR"] == "x"


def test_openclaw_env_none_without_profile() -> None:
    svc = OpenClawLifecycleService()
    assert svc._openclaw_env() is None


def test_onboard_appends_workspace(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "ws"
    _patch_root(monkeypatch, root)
    captured: dict[str, list[str]] = {}

    def fake_runner(command: list[str]) -> CompletedProcess[str]:
        captured["command"] = list(command)
        return CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    svc = OpenClawLifecycleService(runner=fake_runner, profile="agent-1")
    svc.onboard(auth_choice="deepseek-api-key", api_key="k")

    cmd = captured["command"]
    assert cmd[0] == "openclaw"
    assert cmd[1:3] == ["--profile", "agent-1"]
    # 只断言 --workspace 与其值成对出现，不依赖它在命令中的绝对位置
    assert "--workspace" in cmd
    ws_idx = cmd.index("--workspace")
    assert cmd[ws_idx + 1] == str(
        (root / "agent-workspaces" / "agent-1" / "workspace").resolve()
    )
    assert "--workspace" not in cmd[ws_idx + 2 :]


def test_onboard_without_profile_raises() -> None:
    svc = OpenClawLifecycleService()
    with pytest.raises(OpenClawLifecycleError):
        svc.onboard(auth_choice="deepseek-api-key", api_key="k")
