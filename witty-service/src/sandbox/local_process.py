from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from io import TextIOBase
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from src.sandbox.base import (
    AdapterEndpoint,
    SandboxBackend,
    SandboxHandle,
    SandboxStatus,
    sandbox_not_found,
    sandbox_start_failed,
)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LocalProcessSandboxBackend(SandboxBackend):
    sandbox_type = "local_process"

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        agent_server_app_dir: str | None = None,
        stop_timeout: float = 5.0,
        startup_poll_interval: float = 0.1,
    ) -> None:
        self.host = host
        self.agent_server_app_dir = agent_server_app_dir
        self.stop_timeout = stop_timeout
        self.startup_poll_interval = startup_poll_interval
        self._handles: dict[str, SandboxHandle] = {}
        self._processes: dict[str, Any] = {}

    def start(
        self,
        *,
        agent_id: str,
        workspace_path: str,
        **kwargs: Any,
    ) -> SandboxHandle:
        logger.info(f"[LocalProcessSandbox] Starting sandbox for agent_id: {agent_id}")
        port = int(kwargs.get("port", find_free_port()))
        logger.info(f"[LocalProcessSandbox] Using port: {port}")
        app_dir = self._resolve_agent_server_app_dir()
        logger.info(f"[LocalProcessSandbox] Agent server app dir: {app_dir}")
        command = self._build_command(port=port, app_dir=app_dir)
        logger.info(f"[LocalProcessSandbox] Command: {' '.join(command)}")

        try:
            logger.info(f"[LocalProcessSandbox] Starting process in cwd: {command}")
            logger.info(f"[LocalProcessSandbox] Workspace path: {workspace_path}")
            process = subprocess.Popen(
                command,
                cwd=workspace_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            logger.info(f"[LocalProcessSandbox] Process started with PID: {process.pid} in cwd: {app_dir}")
        except OSError as exc:
            logger.error(f"[LocalProcessSandbox] Failed to start process: {exc}")
            raise sandbox_start_failed(
                sandbox_type=self.sandbox_type,
                message="Failed to start local process sandbox.",
                details={
                    "command": command,
                    "stderr": str(exc),
                },
            ) from exc

        time.sleep(self.startup_poll_interval)
        returncode = process.poll()
        logger.info(f"[LocalProcessSandbox] Initial poll returncode: {returncode}")
        if returncode is not None:
            stderr = self._read_stderr(process)
            logger.error(f"[LocalProcessSandbox] Process exited immediately: returncode={returncode}, stderr={stderr}")
            raise sandbox_start_failed(
                sandbox_type=self.sandbox_type,
                message="Local process sandbox exited immediately after startup.",
                details={
                    "command": command,
                    "stderr": stderr,
                    "returncode": returncode,
                },
            )

        sandbox_id = str(uuid4())
        base_url = f"http://{self.host}:{port}"
        logger.info(f"[LocalProcessSandbox] Sandbox ID: {sandbox_id}, base_url: {base_url}")
        handle = SandboxHandle(
            sandbox_id=sandbox_id,
            agent_id=agent_id,
            workspace_path=workspace_path,
            metadata={
                "pid": process.pid,
                "port": port,
                "base_url": base_url,
                "command": command,
                "agent_server_app_dir": app_dir,
            },
        )
        self._handles[sandbox_id] = handle
        self._processes[sandbox_id] = process
        logger.info(f"[LocalProcessSandbox] Sandbox started successfully, returning handle")
        return handle

    def stop(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        sandbox_handle = self._resolve_handle(handle)
        process = self._processes.get(sandbox_handle.sandbox_id)
        if process is None or process.poll() is not None:
            return

        process.terminate()
        try:
            process.wait(timeout=self.stop_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self.stop_timeout)

    def status(self, handle: SandboxHandle | str, **kwargs: Any) -> SandboxStatus:
        sandbox_handle = self._resolve_handle(handle)
        process = self._processes.get(sandbox_handle.sandbox_id)
        if process is None:
            return SandboxStatus.stopped
        if process.poll() is None:
            return SandboxStatus.running
        return SandboxStatus.stopped

    def endpoint(
        self, handle: SandboxHandle | str, **kwargs: Any
    ) -> AdapterEndpoint:
        sandbox_handle = self._resolve_handle(handle)
        base_url = str(sandbox_handle.metadata["base_url"])
        return AdapterEndpoint(base_url=base_url, health_url=f"{base_url}/v1/ping")

    def cleanup(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        sandbox_handle = self._resolve_handle(handle)
        self.stop(sandbox_handle, **kwargs)
        self._processes.pop(sandbox_handle.sandbox_id, None)
        self._handles.pop(sandbox_handle.sandbox_id, None)

    def _build_command(self, *, port: int, app_dir: str) -> list[str]:
        return [
            sys.executable,
            "-m",
            "uvicorn",
            "witty_agent_server.app:create_app",
            "--factory",
            "--app-dir",
            app_dir,
            "--host",
            self.host,
            "--port",
            str(port),
        ]

    def _resolve_agent_server_app_dir(self) -> str:
        if not self.agent_server_app_dir:
            raise sandbox_start_failed(
                sandbox_type=self.sandbox_type,
                message=(
                    "Local process sandbox requires agent_server_app_dir or "
                    "WITTY_AGENT_SERVER_APP_DIR to be configured."
                ),
                details={"env_var": "WITTY_AGENT_SERVER_APP_DIR"},
            )

        path = Path(self.agent_server_app_dir).expanduser().resolve(strict=False)
        if not path.is_dir():
            raise sandbox_start_failed(
                sandbox_type=self.sandbox_type,
                message="Configured agent server app dir does not exist.",
                details={
                    "agent_server_app_dir": str(path),
                    "env_var": "WITTY_AGENT_SERVER_APP_DIR",
                },
            )
        return str(path)

    def _resolve_handle(self, handle: SandboxHandle | str) -> SandboxHandle:
        sandbox_id = handle.sandbox_id if isinstance(handle, SandboxHandle) else handle
        try:
            return self._handles[sandbox_id]
        except KeyError as exc:
            raise sandbox_not_found(
                sandbox_type=self.sandbox_type,
                sandbox_id=sandbox_id,
            ) from exc

    @staticmethod
    def _read_stderr(process: Any) -> str:
        stderr = getattr(process, "stderr", None)
        if stderr is None:
            return ""
        if isinstance(stderr, TextIOBase):
            return stderr.read()
        read = getattr(stderr, "read", None)
        if callable(read):
            output = read()
            return output if isinstance(output, str) else str(output)
        return ""
