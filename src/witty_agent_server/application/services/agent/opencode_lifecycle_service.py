from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from subprocess import Popen
from typing import Any

import httpx

from witty_agent_server.application.services.agent._process_utils import (
    _KILL_TIMEOUT_SECONDS,
    _STDERR_DRAIN_JOIN_TIMEOUT,
    _STOP_TIMEOUT_SECONDS,
    port_is_listening,
    start_stderr_drainer,
)
from witty_agent_server.infra.clients.opencode_client import (
    OpenCodeClient,
    OpenCodeClientError,
)
from witty_service.config import get_settings
from witty_service.workspace_paths import agent_workspace_path

logger = logging.getLogger(__name__)


_HEALTH_TIMEOUT = 3.0
_STARTUP_DEADLINE_SECONDS = 30.0
_STARTUP_POLL_INTERVAL = 1.0


# openclaw ``compatibility`` → opencode ``npm`` (AI SDK 包) 映射。
#   openai            → /v1/chat/completions
#   anthropic         → Anthropic Messages 格式
_COMPATIBILITY_TO_NPM: dict[str, str] = {
    "openai": "@ai-sdk/openai-compatible",
    "anthropic": "@ai-sdk/anthropic",
}


def _build_opencode_model_config(
    *,
    model_provider: str,
    model_name: str | None,
    api_key: str,
    api_base_url: str | None,
    compatibility: str | None = None,
) -> dict[str, Any] | None:
    """构建 opencode model/provider 配置 dict,用于写入 XDG 配置文件。

    依据 opencode 文档 (https://opencode.ai/docs/providers#custom):

    - 内置 provider(openai/anthropic 等):仅设置 ``options.apiKey``
    - 自定义 endpoint(有 ``api_base_url``):设置 ``npm``、``options.baseURL``、
      ``models``,``npm`` 按 ``compatibility`` 选择 AI SDK 包:
        * ``openai`` / 默认 → ``@ai-sdk/openai-compatible``(``/v1/chat/completions``)
        * ``anthropic`` → ``@ai-sdk/anthropic``(Anthropic Messages 格式)
    - ``model`` 字段格式为 ``"provider_id/model_id"``
    """
    if not model_provider or not model_name:
        logger.warning(
            "opencode model config skipped: missing model_provider=%r or model_name=%r",
            model_provider,
            model_name,
        )
        return None

    provider_config: dict[str, Any] = {}
    options: dict[str, Any] = {}

    if api_base_url:
        provider_config["npm"] = _COMPATIBILITY_TO_NPM.get(
            compatibility or "openai", "@ai-sdk/openai-compatible"
        )
        options["baseURL"] = api_base_url
        provider_config["models"] = {model_name: {"name": model_name}}

    if api_key:
        options["apiKey"] = api_key

    if options:
        provider_config["options"] = options

    if not provider_config:
        return None

    return {
        "model": f"{model_provider}/{model_name}",
        "provider": {model_provider: provider_config},
    }

def _to_opencode_mcp_config(config: dict[str, Any]) -> dict[str, Any]:
    """将 MCP 配置 dict 转换为 opencode serve 可接受的格式。

    转换后输出:
        {"type": "local", "command": ["npx", "-y", "..."], "environment": {...}, "enabled": true}
    """
    # 推断 opencode type
    oc_type: str = "remote" if "url" in config else "local"
    oc_config: dict[str, Any] = {"type": oc_type}

    if oc_type == "local":
        command = config.get("command")
        args = config.get("args")
        if isinstance(command, str):
            merged = [command]
            if isinstance(args, list):
                merged.extend(str(a) for a in args)
            oc_config["command"] = merged
        elif isinstance(command, list):
            merged = list(command)
            if isinstance(args, list):
                merged.extend(str(a) for a in args)
            oc_config["command"] = merged
        else:
            logger.warning(
                "MCP server config has invalid 'command' field (type=%s), "
                "server will not be functional. config keys: %s",
                type(command).__name__,
                list(config.keys()),
            )
            oc_config["enabled"] = False
            return oc_config
        if "env" in config:
            oc_config["environment"] = config["env"]
        if "cwd" in config:
            oc_config["cwd"] = config["cwd"]
    else:
        # remote
        if "url" in config:
            oc_config["url"] = config["url"]
        if "headers" in config:
            oc_config["headers"] = config["headers"]

    oc_config["enabled"] = config.get("enabled", True)

    return oc_config


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

    本类持有 ``profile`` 与 ``_serve_process``。
    HTTP 探测/停止通过 ``self._client.http_client()`` 取带 Basic Auth 的 client
    ``http_client()`` 返回的是长持有实例，**禁止 ``with``**。

    ``stderr`` 走 daemon thread 持续 drain 到 logger,避免长跑下 PIPE 缓冲
    (~64KB)写满导致子进程阻塞。
    """

    def __init__(
        self,
        client: OpenCodeClient,
        *,
        profile: str | None = None,
    ) -> None:
        self._client = client
        self._profile = profile
        self._serve_process: Popen[str] | None = None
        self._stderr_drainer: threading.Thread | None = None
        self._model_config: dict[str, Any] | None = None
        self._config_lock: threading.Lock = threading.Lock()

    @property
    def client(self) -> OpenCodeClient:
        return self._client

    @property
    def server_url(self) -> str:
        return self._client.server_url

    @property
    def serve_port(self) -> int:
        return self._client.serve_port

    def update_config(
        self,
        *,
        profile: str | None = None,
    ) -> None:
        """运行时更新进程环境参数。"""
        if profile is not None:
            self._profile = profile

    def configure_model(
        self,
        *,
        model_provider: str,
        model_name: str | None,
        api_key: str,
        api_base_url: str | None,
        compatibility: str | None = None,
    ) -> None:
        """配置 opencode 使用的模型与凭据。

        将 model/provider 配置存储为 dict,供 ``start_server()`` 写入
        XDG 配置文件。
        """
        with self._config_lock:
            self._model_config = _build_opencode_model_config(
                model_provider=model_provider,
                model_name=model_name,
                api_key=api_key,
                api_base_url=api_base_url,
                compatibility=compatibility,
            )

    @property
    def instance_config_home(self) -> Path | None:
        """返回 XDG_CONFIG_HOME 路径"""
        if not self._profile:
            return None
        return agent_workspace_path(self._profile)

    def _opencode_config_path(self) -> Path | None:
        """返回 opencode 配置文件完整路径。

        opencode 自动在 ``$XDG_CONFIG_HOME`` 后追加 ``/opencode``
        """
        config_home = self.instance_config_home
        if config_home is None:
            return None
        return config_home / "opencode" / "opencode.json"

    def _read_config_disk(self) -> dict[str, Any]:
        path = self._opencode_config_path()
        if path is None or not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read opencode config from %s: %s", path, exc)
            return {}

    def _write_config_disk(self, config: dict[str, Any]) -> None:
        path = self._opencode_config_path()
        if path is None:
            logger.warning("Cannot write opencode config: no profile configured")
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(config, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.info("opencode config written to disk: %s", path)
        except OSError as exc:
            logger.error("Failed to write opencode config to %s: %s", path, exc)
            raise

    def _merge_model_into_disk_config(self) -> None:
        """将 model/provider 配置合并到 XDG 配置文件,保留已有的 mcp 等字段。"""
        if self._model_config is None:
            return
        with self._config_lock:
            current = self._read_config_disk()
            current["model"] = self._model_config["model"]
            current["provider"] = self._model_config["provider"]
            self._write_config_disk(current)

    def mcp_set(self, name: str, config: dict[str, Any]) -> None:
        """动态添加并持久化 MCP 配置"""
        if self._opencode_config_path() is None:
            raise OpenCodeLifecycleError(
                action="mcp_set",
                message="Cannot persist MCP config: no profile configured",
            )

        oc_config = _to_opencode_mcp_config(config)

        # ---- XDG 磁盘(持久化优先) ----
        with self._config_lock:
            current = self._read_config_disk()
            mcp_cfg = current.get("mcp", {})
            if not isinstance(mcp_cfg, dict):
                mcp_cfg = {}
            mcp_cfg[name] = oc_config
            current["mcp"] = mcp_cfg
            self._write_config_disk(current)

        # ---- HTTP API(运行时,best-effort) ----
        try:
            self._client.post_mcp_disconnect(name)
        except OpenCodeClientError as exc:
            if exc.status != 404:
                logger.warning("mcp disconnect before set failed: %s", exc)
        except Exception:
            logger.debug("mcp disconnect failed (serve may not be running)", exc_info=True)

        try:
            self._client.post_mcp_add(name, oc_config)
        except OpenCodeClientError as exc:
            logger.warning("mcp add via HTTP failed (will apply on restart): %s", exc)
        except Exception:
            logger.debug("mcp add failed (serve may not be running)", exc_info=True)

    def mcp_unset(self, name: str) -> None:
        """断开并移除 MCP 配置"""
        if self._opencode_config_path() is None:
            raise OpenCodeLifecycleError(
                action="mcp_unset",
                message="Cannot remove MCP config: no profile configured",
            )

        # ---- XDG 磁盘(持久化优先) ----
        with self._config_lock:
            current = self._read_config_disk()
            mcp_cfg = current.get("mcp", {})
            if isinstance(mcp_cfg, dict) and name in mcp_cfg:
                del mcp_cfg[name]
                current["mcp"] = mcp_cfg
                self._write_config_disk(current)

        # ---- HTTP API(运行时,best-effort) ----
        try:
            self._client.post_mcp_disconnect(name)
        except OpenCodeClientError as exc:
            if exc.status != 404:
                logger.warning("mcp disconnect failed: %s", exc)
        except Exception:
            logger.debug("mcp disconnect failed (serve may not be running)", exc_info=True)

    def start_server(self) -> None:
        """启动 ``opencode serve`` 子进程。

        在启动前将 model/provider 配置写入 XDG 配置文件,
        opencode serve 启动时会从 XDG 配置文件读取全量配置。
        """
        self._stop_serve_process()

        env = {
            **os.environ,
            "OPENCODE_PERMISSION": '{"*":"allow"}',
        }
        if self._client.password:
            env["OPENCODE_SERVER_PASSWORD"] = self._client.password

        if self._profile:
            self._setup_xdg_env(env, self._profile)

        # 将 model/provider 写入 XDG 配置文件
        self._merge_model_into_disk_config()

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

    def _setup_xdg_env(self, env: dict[str, str], profile: str) -> None:
        """为指定 *profile* 填充 XDG 环境变量。

        - ``XDG_CONFIG_HOME`` 设为 agent workspace 目录,使 opencode 配置文件
          与 AI 工作区重叠,AI 的 ``glob **/opencode.json*`` 可直接发现配置。
        - 其他 XDG 目录(data / state / cache)仍位于
          ``<WITTY_WORKSPACE_ROOT>/opencode-instances/<profile>/`` 下做实例隔离。
        opencode 会自动在每个 XDG 路径后追加 ``/opencode``，例如
        ``$XDG_DATA_HOME/opencode/opencode.db``。
        """

        workspace_root = get_settings().workspace.root_path()
        inst_root = workspace_root / "opencode-instances" / profile
        config_home = agent_workspace_path(profile, root=workspace_root)

        xdg_dirs: dict[str, Path] = {
            "XDG_DATA_HOME": inst_root / "data",
            "XDG_STATE_HOME": inst_root / "state",
            "XDG_CONFIG_HOME": config_home,
            "XDG_CACHE_HOME": inst_root / "cache",
        }

        for env_var, dir_path in xdg_dirs.items():
            try:
                dir_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise OpenCodeServeStartError(
                    message=(
                        f"Failed to create XDG directory {dir_path} "
                        f"(profile={profile}): {exc}"
                    )
                ) from exc
            env[env_var] = str(dir_path)
            logger.debug(
                "opencode XDG isolation: %s=%s (profile=%s)",
                env_var,
                dir_path,
                profile,
            )



__all__: Sequence[str] = (
    "OpenCodeLifecycleError",
    "OpenCodeLifecycleService",
    "OpenCodeServeStartError",
)
