from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path


logger = logging.getLogger(__name__)


class ConflictReporterManager:
    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._url = ""
        self._owns_process = False

    @property
    def url(self) -> str:
        return self._url

    def start(self, *, enabled: bool) -> str:
        self.stop()
        if not enabled:
            return ""

        existing_url = os.environ.get("CONFLICT_REPORTER_URL", "").strip()
        if existing_url:
            self._url = existing_url
            self._owns_process = False
            return self._url

        opencode = self._resolve_opencode()
        if opencode is None:
            logger.warning("opencode 不在 PATH 中，冲突报告将回退为普通 opencode run。")
            return ""

        port = self._allocate_local_port()
        process = subprocess.Popen(
            [
                str(opencode),
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            self._wait_for_tcp("127.0.0.1", port)
        except Exception as error:
            logger.warning("启动 conflict reporter 失败，将回退为普通 opencode run: %s", error)
            self._terminate_process(process, timeout_seconds=3)
            return ""

        self._process = process
        self._owns_process = True
        self._url = f"http://127.0.0.1:{port}"
        return self._url

    def stop(self) -> None:
        process = self._process
        owns_process = self._owns_process
        self._process = None
        self._url = ""
        self._owns_process = False
        if process is None or not owns_process:
            return
        self._terminate_process(process, timeout_seconds=5)

    @staticmethod
    def _resolve_opencode() -> Path | None:
        candidates = []
        candidate = shutil.which("opencode")
        if candidate:
            candidates.append(candidate)
        candidates.append(str(Path.home() / ".npm-global" / "bin" / "opencode"))
        for item in candidates:
            path = Path(item).expanduser().resolve()
            if path.exists():
                return path
        return None

    @staticmethod
    def _allocate_local_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _wait_for_tcp(host: str, port: int, timeout_seconds: float = 10.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        last_error: OSError | None = None
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.25):
                    return
            except OSError as error:
                last_error = error
                time.sleep(0.1)
        raise RuntimeError(f"等待 conflict reporter 启动超时: {last_error}")

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str], timeout_seconds: float) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
