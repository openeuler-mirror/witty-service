from __future__ import annotations

import logging
import os
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from subprocess import Popen

import httpx

from witty_agent_server.application.services.agent._process_utils import (
    _KILL_TIMEOUT_SECONDS,
    _STDERR_DRAIN_JOIN_TIMEOUT,
    _STOP_TIMEOUT_SECONDS,
    port_is_listening,
    start_stderr_drainer,
)
from witty_agent_server.infra.clients.opencode_client import OpenCodeClient


logger = logging.getLogger(__name__)


_DEFAULT_CONFIG_DIR = "~/.config/opencode"
_HEALTH_TIMEOUT = 3.0
_STARTUP_DEADLINE_SECONDS = 30.0
_STARTUP_POLL_INTERVAL = 1.0


class OpenCodeLifecycleError(Exception):
    """OpenCode 生命周期控制错误。"""

    def __init__(self, *, action: str, message: str) -> None:
        super().__init__(message)
        self.action = action
        self.message = message


class OpenCodeServeStartError(OpenCodeLifecycleError):
    def __init__(self, *, message: str) -> None:
        super().__init__(action="serve start", message=message)


class OpenCodeLifecycleService:
    """OpenCode ``serve`` 子进程生命周期控制。

    本类只持有进程环境字段（``config_dir``）与 ``_serve_process``。
    HTTP 探测/停止通过 ``self._client.http_client()`` 取带 Basic Auth 的 client
    ``http_client()`` 返回的是长持有实例，**禁止 ``with``**。

    ``stderr`` 走 daemon thread 持续 drain 到 logger,避免长跑下 PIPE 缓冲
    (~64KB)写满导致子进程阻塞。
    """

    def __init__(
        self,
        client: OpenCodeClient,
        *,
        config_dir: str | None = None,
    ) -> None:
        self._client = client
        self._config_dir = config_dir if config_dir is not None else _DEFAULT_CONFIG_DIR
        self._serve_process: Popen[str] | None = None
        self._stderr_drainer: threading.Thread | None = None

    @property
    def client(self) -> OpenCodeClient:
        return self._client

    @property
    def server_url(self) -> str:
        return self._client.server_url

    @property
    def serve_port(self) -> int:
        return self._client.serve_port

    def update_config(self, *, config_dir: str | None = None) -> None:
        """运行时更新进程环境参数。"""
        if config_dir is not None:
            self._config_dir = config_dir

    def start_server(self) -> None:
        """启动 ``opencode serve`` 子进程。"""
        self._stop_serve_process()

        config_dir = str(Path(self._config_dir).expanduser())

        env = {
            **os.environ,
            "OPENCODE_CONFIG_DIR": config_dir,
            "OPENCODE_PERMISSION": '{"*":"allow"}',
        }
        if self._client.password:
            env["OPENCODE_SERVER_PASSWORD"] = self._client.password

        serve_port = self._client.serve_port
        command: list[str] = [
            "opencode",
            "serve",
            "--port",
            str(serve_port),
        ]
        logger.info(
            "Starting opencode serve: cmd=%s port=%s",
            command,
            serve_port,
        )
        try:
            self._serve_process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        except FileNotFoundError as exc:
            raise OpenCodeServeStartError(
                message=f"opencode executable not found: {exc}"
            ) from exc

        self._stderr_drainer = start_stderr_drainer(
            self._serve_process,
            logger=logger,
            log_prefix="opencode serve stderr",
            thread_name="opencode-stderr-drainer",
        )

        deadline = time.time() + _STARTUP_DEADLINE_SECONDS
        while time.time() < deadline:
            if self._serve_process.poll() is not None:
                rc = self._serve_process.returncode
                self._stop_serve_process()
                raise OpenCodeServeStartError(
                    message=(
                        f"opencode serve process exited prematurely (rc={rc}); "
                        "see logs for stderr output"
                    )
                )
            if port_is_listening(serve_port) and self.probe_running():
                logger.info("opencode serve started on port %s", serve_port)
                return
            time.sleep(_STARTUP_POLL_INTERVAL)

        self._stop_serve_process()
        raise OpenCodeServeStartError(
            message=(
                f"opencode serve start timed out after {_STARTUP_DEADLINE_SECONDS}s"
            )
        )

    def probe_running(self) -> bool:
        """通过 ``GET /global/health`` 探测 serve 是否健康。"""
        try:
            response = self._client.http_client().get(
                "/global/health", timeout=_HEALTH_TIMEOUT
            )
        except httpx.HTTPError as exc:
            logger.debug("opencode health probe failed: %s", exc)
            return False
        if response.status_code != 200:
            return False
        try:
            body = response.json()
        except (ValueError, httpx.HTTPError):
            logger.debug("opencode health probe returned non-JSON response")
            return False
        return bool(body.get("healthy"))

    def stop(self) -> None:
        """优雅停止：``POST /instance/dispose`` → ``Popen.terminate()`` 兜底。
        
        不变量：调用结束时分机进程**必须**已终止；若 dispose 与 fallback
        终止都未能杀死进程，显式 raise ``OpenCodeLifecycleError``。
        """
        disposed = False
        process = self._serve_process
        try:
            response = self._client.http_client().post(
                "/instance/dispose", timeout=_HEALTH_TIMEOUT
            )
            disposed = response.status_code < 400
        except httpx.HTTPError as exc:
            logger.debug("opencode instance/dispose failed: %s", exc)
        finally:
            self._stop_serve_process()

        if not disposed and process is not None:
            logger.info("opencode dispose unavailable, process terminated as fallback")

        # 不变量校验：若 fallback 终止后进程仍存活，必须显式 raise 让上游感知。
        # 否则 state=STOPPED 与实际脱节（僵尸进程占住端口，下次 start 必失败）。
        if process is not None and process.poll() is None:
            raise OpenCodeLifecycleError(
                action="stop",
                message=(
                    "opencode serve process still alive after dispose + "
                    "terminate + kill fallback"
                ),
            )

    def _stop_serve_process(self) -> None:
        process = self._serve_process
        if process is None:
            return
        self._serve_process = None

        try:
            process.terminate()
            process.wait(timeout=_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
                process.wait(timeout=_KILL_TIMEOUT_SECONDS)
            except Exception:
                logger.exception("Failed to kill opencode serve process, giving up")
        except Exception:
            logger.exception("Failed to terminate opencode serve process")

        drainer = self._stderr_drainer
        self._stderr_drainer = None
        if drainer is not None and drainer.is_alive():
            drainer.join(timeout=_STDERR_DRAIN_JOIN_TIMEOUT)



__all__: Sequence[str] = (
    "OpenCodeLifecycleError",
    "OpenCodeLifecycleService",
    "OpenCodeServeStartError",
)
