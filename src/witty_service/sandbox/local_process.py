from __future__ import annotations

import logging
import os
from math import log
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

from witty_service.sandbox.base import (
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
        """启动本地 witty-agent-server 子进程并返回沙箱句柄。"""
        logger.info(f"[LocalProcessSandbox] Starting sandbox for agent_id: {agent_id}")
        port = int(kwargs.get("port", find_free_port()))
        logger.info(f"[LocalProcessSandbox] Using port: {port}")
        app_dir = self._resolve_agent_server_app_dir()
        logger.info(f"[LocalProcessSandbox] Agent server app dir: {app_dir}")
        command = self._build_command(port=port, app_dir=app_dir)
        logger.info(f"[LocalProcessSandbox] Command: {' '.join(command)}")
        stderr_log_path = self._build_stderr_log_path(workspace_path)
        stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"[LocalProcessSandbox] Stderr log path: {stderr_log_path}")

        # 透传调用方注入的额外环境变量（例如 WITTY_RUNTIME_DEFAULT），
        env = os.environ.copy()
        extra_env = kwargs.get("env")
        if isinstance(extra_env, dict):
            env.update(extra_env)
            logger.info(
                "[LocalProcessSandbox] Merging extra env vars: %s",
                list(extra_env.keys()),
            )

        try:
            logger.info(f"[LocalProcessSandbox] Starting process in cwd: {command}")
            logger.info(f"[LocalProcessSandbox] Workspace path: {workspace_path}")
            with stderr_log_path.open("a", encoding="utf-8") as stderr_file:
                process = subprocess.Popen(
                    command,
                    cwd=workspace_path,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_file,
                    env=env,
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
            stderr = self._read_stderr_log(stderr_log_path)
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
                "stderr_log_path": str(stderr_log_path),
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
        return AdapterEndpoint(base_url=base_url, health_url=f"{base_url}/ping")

    def cleanup(self, handle: SandboxHandle | str, **kwargs: Any) -> None:
        sandbox_handle = self._resolve_handle(handle)
        self.stop(sandbox_handle, **kwargs)
        self._processes.pop(sandbox_handle.sandbox_id, None)
        self._handles.pop(sandbox_handle.sandbox_id, None)

    def _build_command(self, *, port: int, app_dir: str) -> list[str]:
        """构造 witty-agent-server 的启动命令。"""
        witty_service_dir = str(Path(app_dir).parent)
        
        return [
            sys.executable,
            "-m",
            "uvicorn",
            "witty_agent_server.app:create_app",
            "--factory",
            "--app-dir",
            witty_service_dir,
            "--host",
            self.host,
            "--port",
            str(port),
        ]

    def _resolve_agent_server_app_dir(self) -> str:
        # 优先使用显式配置的目录
        if self.agent_server_app_dir:
            path = Path(self.agent_server_app_dir).expanduser().resolve(strict=False)
            if path.is_dir():
                return str(path)
        
        # 自动检测：从当前文件所在位置推断项目根目录
        # 当前文件路径: /root/new/witty-service/src/sandbox/local_process.py
        # 项目根目录: /root/new/witty-service
        # witty_agent_server 目录: /root/new/witty-service/witty_agent_server
        current_file = Path(__file__).resolve()
        project_root = current_file.parent.parent.parent  # src/sandbox -> src -> project_root
        witty_agent_server_dir = project_root / "witty_agent_server"
        
        if witty_agent_server_dir.is_dir():
            return str(witty_agent_server_dir)
        
        raise sandbox_start_failed(
            sandbox_type=self.sandbox_type,
            message=(
                "Cannot find witty_agent_server directory. "
                "Please set WITTY_AGENT_SERVER_APP_DIR environment variable "
                "or ensure witty_agent_server exists in the project root."
            ),
            details={
                "searched_path": str(witty_agent_server_dir),
                "env_var": "WITTY_AGENT_SERVER_APP_DIR",
            },
        )

    @staticmethod
    def _build_stderr_log_path(workspace_path: str) -> Path:
        """为本地子进程生成固定的 stderr 日志文件路径。"""
        logger.info(f"Building stderr log path for workspace: {workspace_path}")
        return Path(workspace_path).expanduser().resolve(strict=False) / "agent-server.stderr.log"

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
    def _read_stderr_log(stderr_log_path: Path) -> str:
        """读取启动失败时写入的 stderr 日志，便于错误排查。"""
        if not stderr_log_path.exists():
            return ""
        return stderr_log_path.read_text(encoding="utf-8")
