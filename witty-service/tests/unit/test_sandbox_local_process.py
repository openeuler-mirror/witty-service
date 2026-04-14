from __future__ import annotations

import io
import subprocess
import sys

import pytest

from src.domain.errors import DomainError
from src.sandbox.base import SandboxStatus
from src.sandbox.factory import create_sandbox_backend
from src.sandbox.local_process import LocalProcessSandboxBackend


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
        "src.sandbox.local_process.find_free_port",
        lambda: 43123,
    )
    monkeypatch.setattr(
        "src.sandbox.local_process.subprocess.Popen",
        fake_popen,
    )

    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(app_dir))

    handle = backend.start(agent_id="agent-1", workspace_path="/tmp/workspace")

    assert popen_calls["command"] == [
        sys.executable,
        "-m",
        "uvicorn",
        "witty_agent_server.app:create_app",
        "--factory",
        "--app-dir",
        str(app_dir.resolve()),
        "--host",
        "127.0.0.1",
        "--port",
        "43123",
    ]
    assert popen_calls["kwargs"] == {
        "cwd": "/tmp/workspace",
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    assert handle.agent_id == "agent-1"
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
        "src.sandbox.local_process.find_free_port",
        lambda: 43124,
    )
    monkeypatch.setattr(
        "src.sandbox.local_process.subprocess.Popen",
        lambda command, **kwargs: fake_process,
    )

    backend = LocalProcessSandboxBackend(
        agent_server_app_dir=str(app_dir),
        stop_timeout=0.5,
    )
    handle = backend.start(agent_id="agent-2", workspace_path="/tmp/workspace")

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
        "src.sandbox.local_process.find_free_port",
        lambda: 43125,
    )
    monkeypatch.setattr(
        "src.sandbox.local_process.subprocess.Popen",
        lambda command, **kwargs: fake_process,
    )

    backend = LocalProcessSandboxBackend(
        agent_server_app_dir=str(app_dir),
        stop_timeout=0.25,
    )
    handle = backend.start(agent_id="agent-3", workspace_path="/tmp/workspace")

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
        "src.sandbox.local_process.find_free_port",
        lambda: 43126,
    )
    monkeypatch.setattr(
        "src.sandbox.local_process.subprocess.Popen",
        lambda command, **kwargs: fake_process,
    )

    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(app_dir))
    handle = backend.start(agent_id="agent-4", workspace_path="/tmp/workspace")

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
        "src.sandbox.local_process.find_free_port",
        lambda: 43128,
    )
    monkeypatch.setattr(
        "src.sandbox.local_process.subprocess.Popen",
        lambda command, **kwargs: fake_process,
    )

    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(app_dir))
    handle = backend.start(agent_id="agent-4b", workspace_path="/tmp/workspace")

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
        "src.sandbox.local_process.find_free_port",
        lambda: 43127,
    )
    monkeypatch.setattr(
        "src.sandbox.local_process.subprocess.Popen",
        lambda command, **kwargs: fake_process,
    )
    monkeypatch.setattr("src.sandbox.local_process.time.sleep", lambda _: None)

    backend = LocalProcessSandboxBackend(agent_server_app_dir=str(app_dir))

    with pytest.raises(DomainError) as exc_info:
        backend.start(agent_id="agent-5", workspace_path="/tmp/workspace")

    assert exc_info.value.code == "SANDBOX_START_FAILED"
    assert exc_info.value.details["stderr"] == "startup boom"
    assert exc_info.value.details["command"] == [
        sys.executable,
        "-m",
        "uvicorn",
        "witty_agent_server.app:create_app",
        "--factory",
        "--app-dir",
        str(app_dir.resolve()),
        "--host",
        "127.0.0.1",
        "--port",
        "43127",
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


def test_local_runtime_start_raises_when_app_dir_missing() -> None:
    backend = LocalProcessSandboxBackend()

    with pytest.raises(DomainError) as exc_info:
        backend.start(agent_id="agent-6", workspace_path="/tmp/workspace")

    assert exc_info.value.code == "SANDBOX_START_FAILED"
    assert "WITTY_AGENT_SERVER_APP_DIR" in exc_info.value.message


def test_local_runtime_factory_returns_backend(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    app_dir = tmp_path / "agent-server"
    app_dir.mkdir()
    monkeypatch.setenv("WITTY_AGENT_SERVER_APP_DIR", str(app_dir))

    backend = create_sandbox_backend("local_process")

    assert isinstance(backend, LocalProcessSandboxBackend)
    assert backend.agent_server_app_dir == str(app_dir)
