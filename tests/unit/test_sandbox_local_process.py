from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

from witty_service.domain.errors import DomainError
from witty_service.sandbox.base import SandboxHandle, SandboxStatus
from witty_service.sandbox.factory import create_sandbox_backend
from witty_service.sandbox.local_process import LocalProcessSandboxBackend


class FakeProcess:
    def __init__(
        self,
        *,
        pid: int = 4321,
        poll_result: int | None = None,
        stderr_text: str = "",
    ) -> None:
        self.pid = pid
        self._poll_result = poll_result
        self.terminate_called = False
        self.kill_called = False
        self.wait_calls: list[float | None] = []
        self.stderr = io.StringIO(stderr_text)

    def poll(self) -> int | None:
        return self._poll_result

    def terminate(self) -> None:
        self.terminate_called = True
        self._poll_result = 0

    def kill(self) -> None:
        self.kill_called = True
        self._poll_result = -9

    def wait(self, timeout: float | None = None) -> int | None:
        self.wait_calls.append(timeout)
        return self._poll_result


class TimeoutOnWaitProcess(FakeProcess):
    def wait(self, timeout: float | None = None) -> int | None:
        self.wait_calls.append(timeout)
        if len(self.wait_calls) == 1:
            raise subprocess.TimeoutExpired(cmd="uv", timeout=timeout or 0)
        return self._poll_result


def test_registered_leader_exempts_its_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.read_proc_stat",
        lambda pid: ("S", str(pid), "start-1") if pid == 4321 else None,
    )

    assert LocalProcessSandboxBackend._pgrp_has_registered_leader(
        "4321", {4321: "start-1"}
    )
    assert not LocalProcessSandboxBackend._pgrp_has_registered_leader(
        "4321", {4321: "old"}
    )
    assert not LocalProcessSandboxBackend._pgrp_has_registered_leader(
        "9999", {4321: "start-1"}
    )


def test_stopping_one_workspace_does_not_touch_another(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = iter([FakeProcess(pid=5101), FakeProcess(pid=5102)])
    ports = iter([41001, 41002])

    monkeypatch.setattr(
        "witty_service.sandbox.local_process.find_free_port", lambda: next(ports)
    )
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.subprocess.Popen",
        lambda command, **kwargs: next(processes),
    )
    monkeypatch.setattr(
        LocalProcessSandboxBackend,
        "_read_process_start_time",
        staticmethod(lambda pid: "start"),
    )
    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(tmp_path))

    handle_a = backend.start(workspace_id="ws_a", workspace_path=str(tmp_path / "a"))
    handle_b = backend.start(workspace_id="ws_b", workspace_path=str(tmp_path / "b"))
    backend.stop(handle_a)

    assert handle_a.workspace_id == "ws_a"
    assert handle_b.workspace_id == "ws_b"
    assert backend._processes[handle_a.sandbox_id].terminate_called is True
    assert backend._processes[handle_b.sandbox_id].terminate_called is False


def test_local_runtime_honors_workspace_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    popen_calls: dict[str, object] = {}
    fake_process = FakeProcess(pid=9876)
    workspace = tmp_path / "workspace"
    log_path = tmp_path / "runtime" / "server.stderr.log"

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        popen_calls["command"] = command
        popen_calls["kwargs"] = kwargs
        return fake_process

    monkeypatch.setattr(
        "witty_service.sandbox.local_process.find_free_port", lambda: 43123
    )
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.subprocess.Popen", fake_popen
    )

    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(tmp_path / "unused"))

    handle = backend.start(
        workspace_id="ws_a",
        workspace_path=str(workspace),
        command=("opencode", "serve", "--port", "{port}"),
        stderr_log_path=str(log_path),
    )

    assert popen_calls["command"] == ["opencode", "serve", "--port", "43123"]
    assert popen_calls["kwargs"]["cwd"] == str(workspace)
    assert handle.metadata["process_marker"] == "opencode"
    assert handle.metadata["stderr_log_path"] == str(log_path)
    assert log_path.exists()


def test_local_runtime_start_builds_expected_command_and_handle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    popen_calls: dict[str, object] = {}
    fake_process = FakeProcess(pid=9876)
    app_dir = tmp_path / "agent-server"
    app_dir.mkdir()

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        popen_calls["command"] = command
        popen_calls["kwargs"] = kwargs
        return fake_process

    monkeypatch.setattr(
        "witty_service.sandbox.local_process.find_free_port",
        lambda: 43123,
    )
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.subprocess.Popen",
        fake_popen,
    )

    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(app_dir))

    handle = backend.start(workspace_id="agent-1", workspace_path="/tmp/workspace")

    assert popen_calls["command"] == [
        sys.executable,
        "-m",
        "uvicorn",
        "witty_agent_server.app:create_app",
        "--factory",
        "--app-dir",
        str(app_dir.resolve().parent),
        "--host",
        "127.0.0.1",
        "--port",
        "43123",
        "--ws-ping-interval",
        "20.0",
        "--ws-ping-timeout",
        "120.0",
    ]
    assert popen_calls["kwargs"]["cwd"] == "/tmp/workspace"
    assert popen_calls["kwargs"]["stdout"] == subprocess.DEVNULL
    assert popen_calls["kwargs"]["text"] is True
    # stderr 传入的是日志文件,不是 subprocess.PIPE
    assert "stderr" in popen_calls["kwargs"]
    assert popen_calls["kwargs"]["stderr"] is not subprocess.PIPE
    assert handle.workspace_id == "agent-1"
    assert handle.workspace_path == "/tmp/workspace"
    assert handle.metadata["pid"] == 9876
    assert handle.metadata["port"] == 43123
    assert handle.metadata["base_url"] == "http://127.0.0.1:43123"


def test_local_runtime_stop_terminates_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    fake_process = FakeProcess(pid=2468)
    app_dir = tmp_path / "agent-server"
    app_dir.mkdir()
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.find_free_port",
        lambda: 43124,
    )
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.subprocess.Popen",
        lambda command, **kwargs: fake_process,
    )

    backend = LocalProcessSandboxBackend(
        agent_server_app_dir=str(app_dir),
        stop_timeout=0.5,
    )
    handle = backend.start(workspace_id="agent-2", workspace_path="/tmp/workspace")

    backend.stop(handle)

    assert fake_process.terminate_called is True
    assert fake_process.kill_called is False
    assert fake_process.wait_calls == [0.5]
    assert backend.status(handle) is SandboxStatus.stopped


def test_local_runtime_stop_kills_process_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    fake_process = TimeoutOnWaitProcess(pid=1357)
    app_dir = tmp_path / "agent-server"
    app_dir.mkdir()
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.find_free_port",
        lambda: 43125,
    )
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.subprocess.Popen",
        lambda command, **kwargs: fake_process,
    )

    backend = LocalProcessSandboxBackend(
        agent_server_app_dir=str(app_dir),
        stop_timeout=0.25,
    )
    handle = backend.start(workspace_id="agent-3", workspace_path="/tmp/workspace")

    backend.stop(handle)

    assert fake_process.terminate_called is True
    assert fake_process.kill_called is True
    assert fake_process.wait_calls == [0.25, 0.25]


def test_local_runtime_status_endpoint_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    fake_process = FakeProcess(pid=1111, poll_result=None)
    app_dir = tmp_path / "agent-server"
    app_dir.mkdir()
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.find_free_port",
        lambda: 43126,
    )
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.subprocess.Popen",
        lambda command, **kwargs: fake_process,
    )

    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(app_dir))
    handle = backend.start(workspace_id="agent-4", workspace_path="/tmp/workspace")

    assert backend.status(handle) is SandboxStatus.running
    assert backend.endpoint(handle).base_url == "http://127.0.0.1:43126"

    fake_process._poll_result = 0
    assert backend.status(handle.sandbox_id) is SandboxStatus.stopped

    fake_process._poll_result = None
    backend.cleanup(handle.sandbox_id)
    assert fake_process.terminate_called is True
    assert handle.sandbox_id not in backend._handles
    assert handle.sandbox_id not in backend._processes


def test_local_runtime_cleanup_keeps_handle_when_stop_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    fake_process = FakeProcess(pid=1112)
    app_dir = tmp_path / "agent-server"
    app_dir.mkdir()
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.find_free_port",
        lambda: 43128,
    )
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.subprocess.Popen",
        lambda command, **kwargs: fake_process,
    )

    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(app_dir))
    handle = backend.start(workspace_id="agent-4b", workspace_path="/tmp/workspace")

    def fail_stop(*_: object, **__: object) -> None:
        raise RuntimeError("stop failed")

    monkeypatch.setattr(backend, "stop", fail_stop)

    with pytest.raises(RuntimeError) as exc_info:
        backend.cleanup(handle.sandbox_id)

    assert str(exc_info.value) == "stop failed"
    assert handle.sandbox_id in backend._handles
    assert handle.sandbox_id in backend._processes
    assert fake_process.terminate_called is False


def test_local_runtime_start_raises_when_process_exits_immediately(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    app_dir = tmp_path / "agent-server"
    app_dir.mkdir()
    fake_process = FakeProcess(pid=9999, poll_result=1, stderr_text="startup boom")

    monkeypatch.setattr(
        "witty_service.sandbox.local_process.find_free_port",
        lambda: 43127,
    )
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.subprocess.Popen",
        lambda command, **kwargs: fake_process,
    )
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.time.sleep", lambda _: None
    )
    # 让 _read_stderr_log 返回模拟的 stderr 内容
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.LocalProcessSandboxBackend._read_stderr_log",
        lambda self, path: "startup boom",
    )

    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(app_dir))

    with pytest.raises(DomainError) as exc_info:
        backend.start(workspace_id="agent-5", workspace_path="/tmp/workspace")

    assert exc_info.value.code == "SANDBOX_START_FAILED"
    assert exc_info.value.details["stderr"] == "startup boom"
    assert exc_info.value.details["command"] == [
        sys.executable,
        "-m",
        "uvicorn",
        "witty_agent_server.app:create_app",
        "--factory",
        "--app-dir",
        str(app_dir.resolve().parent),
        "--host",
        "127.0.0.1",
        "--port",
        "43127",
        "--ws-ping-interval",
        "20.0",
        "--ws-ping-timeout",
        "120.0",
    ]


@pytest.mark.parametrize("operation", ["stop", "status", "endpoint", "cleanup"])
def test_local_runtime_unknown_handle_raises_runtime_not_found(
    operation: str,
    tmp_path: pytest.TempPathFactory,
) -> None:
    app_dir = tmp_path / "agent-server"
    app_dir.mkdir()
    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(app_dir))

    with pytest.raises(DomainError) as exc_info:
        getattr(backend, operation)("missing-runtime")

    assert exc_info.value.code == "SANDBOX_NOT_FOUND"
    assert exc_info.value.details["sandbox_id"] == "missing-runtime"


def test_local_runtime_start_raises_when_app_dir_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 通过 monkeypatch _resolve_agent_server_app_dir 让它抛错(模拟自动检测失败)
    from witty_service.sandbox.base import sandbox_start_failed

    def raise_start_failed(self):  # type: ignore[no-untyped-def]
        raise sandbox_start_failed(
            sandbox_type=self.sandbox_type,
            message="WITTY_AGENT_SERVER_APP_DIR is not configured and auto-detection failed.",
            details={"env_var": "WITTY_AGENT_SERVER_APP_DIR"},
        )

    monkeypatch.setattr(
        "witty_service.sandbox.local_process.LocalProcessSandboxBackend._resolve_agent_server_app_dir",
        raise_start_failed,
    )

    backend = LocalProcessSandboxBackend()

    with pytest.raises(DomainError) as exc_info:
        backend.start(workspace_id="agent-6", workspace_path="/tmp/workspace")

    assert exc_info.value.code == "SANDBOX_START_FAILED"
    assert "WITTY_AGENT_SERVER_APP_DIR" in exc_info.value.message


def test_local_runtime_factory_returns_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app_dir = tmp_path / "agent-server"
    app_dir.mkdir()
    monkeypatch.setenv("WITTY_AGENT_SERVER_APP_DIR", str(app_dir))
    # 重置 settings 单例缓存,使 factory 重新读取环境变量
    import witty_service.config as _config

    monkeypatch.setattr(_config, "_settings", None)

    backend = create_sandbox_backend("local_process")

    assert isinstance(backend, LocalProcessSandboxBackend)
    assert backend.agent_server_app_dir == str(app_dir.resolve())


def test_find_handle_by_workspace_scans_proc_and_confirms_absence(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """登记表为空时按 /proc 归属回查；扫描成功但无匹配才算已确证不存在。"""
    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(tmp_path))
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.os.listdir",
        lambda _: ["1234", "self"],
    )
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.read_proc_cmdline",
        lambda pid, *, strict=False: (
            ["python", "witty_agent_server.app:create_app"] if pid == 1234 else None
        ),
    )
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.read_proc_stat",
        lambda pid, *, strict=False: ("S", "1234", "start-1") if pid == 1234 else None,
    )
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.LocalProcessSandboxBackend._cwd_under",
        staticmethod(lambda pid, root, *, strict=False: True),
    )

    handle = backend.find_handle_by_workspace(
        workspace_id="agent-1", workspace_path=str(tmp_path)
    )

    assert handle is not None
    assert handle.sandbox_id == "untracked-1234"
    assert handle.metadata == {
        "pid": 1234,
        "pgid": 1234,
        "process_start_time": "start-1",
        "process_marker": "witty_agent_server.app:create_app",
    }

    monkeypatch.setattr(
        "witty_service.sandbox.local_process.read_proc_cmdline",
        lambda pid, *, strict=False: None,
    )
    assert (
        backend.find_handle_by_workspace(
            workspace_id="agent-1", workspace_path=str(tmp_path)
        )
        is None
    )


def test_find_handle_by_workspace_raises_when_proc_scan_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """扫描失败必须抛错，不能把「看不到进程」当成「没有进程」。"""
    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(tmp_path))

    def _boom(_: str) -> list[str]:
        raise OSError("proc unavailable")

    monkeypatch.setattr("witty_service.sandbox.local_process.os.listdir", _boom)

    with pytest.raises(OSError):
        backend.find_handle_by_workspace(
            workspace_id="agent-1", workspace_path=str(tmp_path)
        )


def test_find_handle_by_workspace_raises_when_cmdline_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """进程存在但读不到 cmdline 时是 unknown，不能当成已确证不存在。"""
    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(tmp_path))
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.os.listdir", lambda _: ["1234"]
    )

    def _denied(_: Path) -> bytes:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_bytes", _denied)

    with pytest.raises(PermissionError):
        backend.find_handle_by_workspace(
            workspace_id="agent-1", workspace_path=str(tmp_path)
        )


def test_find_handle_by_workspace_raises_when_cwd_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """进程存在但读不到 cwd 时是 unknown，不能当成已确证不存在。"""
    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(tmp_path))
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.os.listdir", lambda _: ["1234"]
    )
    monkeypatch.setattr(
        "witty_service.sandbox.local_process.read_proc_cmdline",
        lambda pid, *, strict=False: ["python", "witty_agent_server.app:create_app"],
    )

    def _denied(_: str) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr("witty_service.sandbox.local_process.os.readlink", _denied)

    with pytest.raises(PermissionError):
        backend.find_handle_by_workspace(
            workspace_id="agent-1", workspace_path=str(tmp_path)
        )


def test_cwd_under_only_accepts_workspace_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """归属过滤只接受 workspace 目录树内的 cwd。"""
    workspace = tmp_path / "agent" / "workspace"
    outside = tmp_path / "other"
    workspace.mkdir(parents=True)
    outside.mkdir()

    monkeypatch.setattr(
        "witty_service.sandbox.local_process.os.readlink", lambda _: str(outside)
    )
    assert not LocalProcessSandboxBackend._cwd_under(1234, workspace)

    monkeypatch.setattr(
        "witty_service.sandbox.local_process.os.readlink", lambda _: str(workspace)
    )
    assert LocalProcessSandboxBackend._cwd_under(1234, workspace)


def test_status_probes_pid_for_untracked_handle(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """未登记句柄不能直接返回 stopped：按 pid / start_time 探活。"""
    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(tmp_path))
    handle = SandboxHandle(
        sandbox_id="untracked-1234",
        workspace_id="agent-1",
        workspace_path=str(tmp_path),
        metadata={"pid": 1234, "process_start_time": "start-1"},
    )

    monkeypatch.setattr(backend, "_pid_is_our_agent_server", lambda pid, metadata: True)
    assert backend.status(handle) == SandboxStatus.running

    monkeypatch.setattr(
        backend, "_pid_is_our_agent_server", lambda pid, metadata: False
    )
    assert backend.status(handle) == SandboxStatus.stopped


def test_write_runtime_files_accepts_symlinked_runtime_home(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(tmp_path))

    backend.write_runtime_files(
        runtime_home=str(link), files={"opencode/opencode.json": "{}"}
    )

    assert (real / "opencode" / "opencode.json").read_text(encoding="utf-8") == "{}"


def test_write_runtime_files_rejects_symlink_escape(tmp_path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    (home / "link").symlink_to(outside, target_is_directory=True)
    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(tmp_path))

    with pytest.raises(ValueError, match="escapes runtime home"):
        backend.write_runtime_files(
            runtime_home=str(home), files={"link/escape.txt": "x"}
        )
