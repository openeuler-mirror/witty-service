from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

import witty_agent_server.application.services.agent.openclaw_lifecycle_service as lc_mod
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
        lambda: SimpleNamespace(workspace=SimpleNamespace(root_path=lambda: root)),
    )


def _patch_profile_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """将 lifecycle 模块内解析的 openclaw profile 主目录重定向到 home 下。"""
    monkeypatch.setattr(
        lc_mod, "openclaw_profile_home", lambda profile: home / f".openclaw-{profile}"
    )


def test_workspace_path_derives_from_profile(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "ws"
    _patch_root(monkeypatch, root)
    svc = OpenClawLifecycleService(profile="agent-99")
    assert (
        svc._workspace_path()
        == (root / "agent-workspaces" / "agent-99" / "workspace").resolve()
    )


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


# ---------------------------------------------------------------------------
# stop(wipe) 语义拆分（Phase 0）
# ---------------------------------------------------------------------------


def test_stop_without_wipe_never_runs_uninstall(tmp_path: Path, monkeypatch) -> None:
    """pause 语义：只终止 gateway 进程，绝不调用 uninstall（防数据丢失）。"""
    _patch_root(monkeypatch, tmp_path / "ws")
    _patch_profile_home(monkeypatch, tmp_path)
    profile_home = tmp_path / ".openclaw-agent-1"
    profile_home.mkdir()
    (profile_home / "openclaw.json").write_text("{}", encoding="utf-8")

    commands: list[list[str]] = []

    def fake_runner(command: list[str]) -> CompletedProcess[str]:
        commands.append(list(command))
        return CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    svc = OpenClawLifecycleService(runner=fake_runner, profile="agent-1")
    # 无 Popen 句柄且无 pidfile → _kill_orphan_gateway 直接返回
    svc.stop()  # 默认 wipe=False

    assert commands == []
    assert (profile_home / "openclaw.json").exists()


def test_stop_with_wipe_runs_uninstall_all(tmp_path: Path, monkeypatch) -> None:
    """delete 语义：执行 uninstall --all 完整卸载。"""
    _patch_root(monkeypatch, tmp_path / "ws")
    captured: dict[str, list[str]] = {}

    def fake_runner(command: list[str]) -> CompletedProcess[str]:
        captured["command"] = list(command)
        return CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    svc = OpenClawLifecycleService(runner=fake_runner, profile="agent-1")
    svc.stop(wipe=True)

    cmd = captured["command"]
    assert cmd[0:3] == ["openclaw", "--profile", "agent-1"]
    assert cmd[3:] == ["uninstall", "--yes", "--non-interactive", "--all"]


def test_profile_is_provisioned(tmp_path: Path, monkeypatch) -> None:
    _patch_profile_home(monkeypatch, tmp_path)
    svc = OpenClawLifecycleService(profile="agent-1")
    assert svc.profile_is_provisioned() is False

    (tmp_path / ".openclaw-agent-1").mkdir()
    (tmp_path / ".openclaw-agent-1" / "openclaw.json").write_text(
        "{}", encoding="utf-8"
    )
    assert svc.profile_is_provisioned() is True

    assert OpenClawLifecycleService().profile_is_provisioned() is False


# ---------------------------------------------------------------------------
# pidfile：孤儿 gateway 定位与回收
# ---------------------------------------------------------------------------


class _FakeGatewayProcess:
    """Popen 替身：stderr=None → 不启动 drainer 线程。"""

    pid = 4321
    stderr = None

    def poll(self) -> None:
        return None


def test_start_server_writes_pidfile(tmp_path: Path, monkeypatch) -> None:
    _patch_root(monkeypatch, tmp_path / "ws")
    svc = OpenClawLifecycleService(profile="agent-1", gateway_port=39487)
    monkeypatch.setattr(
        lc_mod.subprocess, "Popen", lambda *a, **kw: _FakeGatewayProcess()
    )
    monkeypatch.setattr(lc_mod, "port_is_listening", lambda port: True)

    svc.start_server()

    pidfile = svc._gateway_pidfile_path()
    assert pidfile is not None
    assert pidfile.read_text(encoding="utf-8") == "4321"


def test_stop_gateway_process_removes_pidfile(tmp_path: Path, monkeypatch) -> None:
    _patch_root(monkeypatch, tmp_path / "ws")
    svc = OpenClawLifecycleService(profile="agent-1")
    pidfile = svc._gateway_pidfile_path()
    assert pidfile is not None
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("999", encoding="utf-8")

    killed: list[int] = []
    monkeypatch.setattr(svc, "_kill_orphan_gateway", lambda: killed.append(1))

    svc._stop_gateway_process()

    assert not pidfile.exists()
    assert killed == [1]


def test_kill_orphan_gateway_ignores_pid_environ_mismatch(
    tmp_path: Path, monkeypatch
) -> None:
    """environ 校验失败（PID 被复用/pidfile 过期）时不得发信号。"""
    _patch_root(monkeypatch, tmp_path / "ws")
    svc = OpenClawLifecycleService(profile="agent-1")
    pidfile = svc._gateway_pidfile_path()
    assert pidfile is not None
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("4242", encoding="utf-8")

    monkeypatch.setattr(svc, "_pid_is_gateway_for_workspace", lambda pid: False)
    signals: list[int] = []
    monkeypatch.setattr(lc_mod.os, "kill", lambda pid, sig: signals.append(pid))

    svc._kill_orphan_gateway()

    assert signals == []


def test_kill_orphan_gateway_terminates_matching_pid(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_root(monkeypatch, tmp_path / "ws")
    svc = OpenClawLifecycleService(profile="agent-1")
    pidfile = svc._gateway_pidfile_path()
    assert pidfile is not None
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text("4242", encoding="utf-8")

    monkeypatch.setattr(svc, "_pid_is_gateway_for_workspace", lambda pid: True)
    monkeypatch.setattr(svc, "_pid_alive", lambda pid: False)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(lc_mod.os, "kill", lambda pid, sig: signals.append((pid, sig)))

    svc._kill_orphan_gateway()

    assert signals == [(4242, lc_mod.signal.SIGTERM)]
