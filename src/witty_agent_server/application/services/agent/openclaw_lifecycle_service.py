import contextlib
import json
import logging
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from subprocess import CompletedProcess, Popen, run

from witty_agent_server.application.services.agent._process_utils import (
    _KILL_TIMEOUT_SECONDS,
    _STDERR_DRAIN_JOIN_TIMEOUT,
    _STOP_TIMEOUT_SECONDS,
    port_is_listening,
    start_stderr_drainer,
)
from witty_service.workspace_paths import agent_workspace_path, openclaw_profile_home

logger = logging.getLogger(__name__)


CommandRunner = Callable[[list[str]], CompletedProcess[str]]

#: gateway 进程 pidfile 文件名（存于 agent workspace，与 stderr 日志同目录；
#: pause 保留 workspace，故跨 agent-server 重启仍可定位孤儿 gateway）。
_GATEWAY_PIDFILE_NAME = "gateway.pid"

#: 孤儿 gateway SIGTERM 后的存活复查间隔。
_ORPHAN_POLL_INTERVAL_SECONDS = 0.2

#: start_server 等待 gateway 端口监听的轮询间隔（gateway 实测 ~4s 起监听，
#: 1s 粒度平均多等 0.4s，0.2s 粒度足够省时且轮询开销可忽略）。
_GATEWAY_PORT_POLL_INTERVAL_SECONDS = 0.2


class OpenClawLifecycleError(Exception):
    def __init__(
        self,
        *,
        action: str,
        command: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.action = action
        self.command = tuple(command)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class OpenClawGatewayStatusError(OpenClawLifecycleError):
    def __init__(
        self,
        *,
        command: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(
            action="status",
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            message="openclaw gateway status failed",
        )


class OpenClawGatewayStopError(OpenClawLifecycleError):
    def __init__(
        self,
        *,
        command: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(
            action="stop",
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            message="openclaw gateway stop failed",
        )


class OpenClawGatewayStartError(OpenClawLifecycleError):
    def __init__(
        self,
        *,
        command: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(
            action="start",
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            message="openclaw gateway start failed",
        )


class OpenClawMcpSetError(OpenClawLifecycleError):
    def __init__(
        self,
        *,
        command: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(
            action="mcp set",
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            message="openclaw mcp set failed",
        )


class OpenClawMcpUnsetError(OpenClawLifecycleError):
    def __init__(
        self,
        *,
        command: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(
            action="mcp unset",
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            message="openclaw mcp unset failed",
        )


class OpenClawOnboardError(OpenClawLifecycleError):
    def __init__(
        self,
        *,
        command: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(
            action="onboard",
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            message="openclaw onboard failed",
        )


class OpenClawLifecycleService:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        profile: str | None = None,
        gateway_port: int | None = None,
    ) -> None:
        self._runner: CommandRunner = runner or self._default_runner
        self._profile = profile
        self._gateway_port = gateway_port
        self._gateway_process: Popen[str] | None = None
        self._stderr_drainer: threading.Thread | None = None

    def update_config(
        self,
        *,
        profile: str | None,
        gateway_port: int | None,
    ) -> None:
        self._profile = profile
        self._gateway_port = gateway_port

    def _build_base_command(self) -> list[str]:
        cmd = ["openclaw"]
        if self._profile:
            cmd.extend(["--profile", self._profile])
        return cmd

    def _workspace_path(self) -> Path | None:
        """Agent 专属 witty workspace（与 witty-service agent.workspace_path 同一约定）。"""
        if not self._profile:
            return None
        return agent_workspace_path(self._profile)

    def _openclaw_env(self) -> dict[str, str] | None:
        """为 openclaw 子进程注入 OPENCLAW_WORKSPACE_DIR，作为 env 解析路径的兜底。"""
        workspace = self._workspace_path()
        if workspace is None:
            return None
        return {**os.environ, "OPENCLAW_WORKSPACE_DIR": str(workspace)}

    def probe_running(self) -> bool:
        if not self._profile:
            return False
        result = self._invoke_gateway_command("status")
        if result.returncode != 0:
            return False
        return self._result_indicates_running(result)

    def profile_is_provisioned(self) -> bool:
        """profile 是否已完成过 onboard（存在 openclaw.json 配置）。

        未 provisioned 的 profile 不可能有 gateway 在运行（onboard 先于
        gateway run），调用方可据此跳过 ``probe_running`` 的 ~1.2s CLI 冷启动；
        也是 provider 模板池「全新 profile 才分配」的判定依据。
        """
        if not self._profile:
            return False
        return (openclaw_profile_home(self._profile) / "openclaw.json").exists()

    def stop(self, *, wipe: bool = False) -> None:
        """停止 openclaw gateway。

        - ``wipe=False``（pause 路径）：只终止 gateway 进程，保留 profile 目录
          （含已安装的 provider 插件、openclaw 会话状态与 workspace）。注意
          ``uninstall --all`` 会连 agent workspace 一起删除（数据丢失），且
          强制下次启动重新 npm 安装插件（~17s），因此 pause 绝不可用。
        - ``wipe=True``（delete 路径）：执行 ``uninstall --all`` 完整卸载。
        """
        if not self._profile:
            return

        if not wipe:
            self._stop_gateway_process()
            return

        # 构建卸载命令
        command = [
            *self._build_base_command(),
            "uninstall",
            "--yes",
            "--non-interactive",
            "--all",
        ]

        result = self._run_command(command)
        if result.returncode != 0:
            raise OpenClawLifecycleError(
                action="uninstall",
                command=command,
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                message="openclaw uninstall failed",
            )

        self._stop_gateway_process()

    def start(self) -> None:
        raise NotImplementedError(
            "start() method is deprecated. Use onboard() instead."
        )

    def start_server(self) -> None:
        if not self._profile or not self._gateway_port:
            raise OpenClawLifecycleError(
                action="gateway run",
                command=[],
                returncode=-1,
                stdout="",
                stderr="profile and gateway_port are required",
                message="profile and gateway_port are required for gateway run",
            )

        self._stop_gateway_process()

        command = [
            *self._build_base_command(),
            "gateway",
            "run",
            "--port",
            str(self._gateway_port),
            "--bind",
            "loopback",
        ]
        logger.info("Starting gateway in background: %s", command)
        self._gateway_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=self._openclaw_env(),
        )

        self._stderr_drainer = start_stderr_drainer(
            self._gateway_process,
            logger=logger,
            log_prefix="openclaw gateway stderr",
            thread_name="openclaw-gateway-stderr-drainer",
        )
        self._write_gateway_pidfile(self._gateway_process.pid)

        deadline = time.time() + 30
        while time.time() < deadline:
            if self._gateway_process.poll() is not None:
                rc = self._gateway_process.returncode or -1
                self._stop_gateway_process()
                raise OpenClawLifecycleError(
                    action="gateway run",
                    command=command,
                    returncode=rc,
                    stdout="",
                    stderr="(see logs for stderr output)",
                    message="gateway process exited prematurely",
                )
            if port_is_listening(self._gateway_port):
                logger.info(
                    "Gateway started successfully on port %s", self._gateway_port
                )
                return
            time.sleep(_GATEWAY_PORT_POLL_INTERVAL_SECONDS)

        self._stop_gateway_process()
        raise OpenClawLifecycleError(
            action="gateway run",
            command=command,
            returncode=-1,
            stdout="",
            stderr="",
            message="gateway start timed out after 30 seconds",
        )

    def _stop_gateway_process(self) -> None:
        process = self._gateway_process
        if process is not None:
            self._gateway_process = None

            try:
                process.terminate()
                process.wait(timeout=_STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                    process.wait(timeout=_KILL_TIMEOUT_SECONDS)
                except Exception:
                    logger.exception("Failed to kill gateway process, giving up")
            except Exception:
                logger.exception("Failed to terminate gateway process")
        else:
            # 本进程没有 Popen 句柄：agent-server 重启/崩溃后遗留的孤儿
            # gateway 仍占着端口，若不回收，start_server 会因端口冲突失败
            self._kill_orphan_gateway()

        self._remove_gateway_pidfile()
        drainer = self._stderr_drainer
        self._stderr_drainer = None
        if drainer is not None and drainer.is_alive():
            drainer.join(timeout=_STDERR_DRAIN_JOIN_TIMEOUT)

    def _gateway_pidfile_path(self) -> Path | None:
        workspace = self._workspace_path()
        if workspace is None:
            return None
        return workspace / _GATEWAY_PIDFILE_NAME

    def _write_gateway_pidfile(self, pid: int) -> None:
        pidfile = self._gateway_pidfile_path()
        if pidfile is None:
            return
        try:
            pidfile.parent.mkdir(parents=True, exist_ok=True)
            pidfile.write_text(str(pid), encoding="utf-8")
        except OSError:
            logger.warning(
                "Failed to write gateway pidfile: profile=%s pid=%s",
                self._profile,
                pid,
                exc_info=True,
            )

    def _remove_gateway_pidfile(self) -> None:
        pidfile = self._gateway_pidfile_path()
        if pidfile is None:
            return
        try:
            pidfile.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Failed to remove gateway pidfile: profile=%s", self._profile
            )

    def _kill_orphan_gateway(self) -> None:
        """按 pidfile 回收孤儿 gateway。"""
        pidfile = self._gateway_pidfile_path()
        if pidfile is None or not pidfile.exists():
            return
        try:
            pid = int(pidfile.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            logger.warning(
                "Ignoring unreadable gateway pidfile: profile=%s", self._profile
            )
            return

        if not self._pid_is_gateway_for_workspace(pid):
            # PID 已被回收复用或非本 profile 的 gateway，pidfile 过期。
            return

        logger.info(
            "Killing orphan gateway process: profile=%s pid=%s",
            self._profile,
            pid,
        )
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            logger.warning(
                "Failed to signal orphan gateway: profile=%s pid=%s",
                self._profile,
                pid,
                exc_info=True,
            )
            return

        deadline = time.time() + _STOP_TIMEOUT_SECONDS
        while time.time() < deadline:
            if not self._pid_alive(pid):
                return
            time.sleep(_ORPHAN_POLL_INTERVAL_SECONDS)

        logger.warning(
            "Orphan gateway survived SIGTERM, escalating to SIGKILL: profile=%s pid=%s",
            self._profile,
            pid,
        )
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)

    def _pid_is_gateway_for_workspace(self, pid: int) -> bool:
        """校验 PID 确为本 profile 的 gateway（environ 匹配），防误杀复用 PID。"""
        workspace = self._workspace_path()
        if workspace is None:
            return False
        expected = f"OPENCLAW_WORKSPACE_DIR={workspace}"
        try:
            environ = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            return False
        return expected.encode("utf-8") in environ

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True

    def mcp_set(self, name: str, config: dict[str, object]) -> None:
        if not self._profile:
            raise OpenClawLifecycleError(
                action="mcp set",
                command=[],
                returncode=-1,
                stdout="",
                stderr="profile is required",
                message="profile is required for mcp set",
            )

        config_str = json.dumps(config, ensure_ascii=False)
        command = [*self._build_base_command(), "mcp", "set", name, config_str]
        result = self._run_command(command)
        if result.returncode != 0:
            raise OpenClawMcpSetError(
                command=command,
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )

    def mcp_unset(self, name: str) -> None:
        if not self._profile:
            raise OpenClawLifecycleError(
                action="mcp unset",
                command=[],
                returncode=-1,
                stdout="",
                stderr="profile is required",
                message="profile is required for mcp unset",
            )

        command = [*self._build_base_command(), "mcp", "unset", name]
        result = self._run_command(command)
        if result.returncode != 0:
            raise OpenClawMcpUnsetError(
                command=command,
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )

    def onboard(
        self,
        *,
        auth_choice: str,
        api_key: str,
        install_daemon: bool = False,
        skip_channels: bool = True,
        skip_search: bool = True,
        skip_hooks: bool = True,
        skip_health: bool = False,
        custom_base_url: str | None = None,
        custom_model_id: str | None = None,
        custom_compatibility: str | None = None,
    ) -> None:
        if not self._profile:
            raise OpenClawLifecycleError(
                action="onboard",
                command=[],
                returncode=-1,
                stdout="",
                stderr="profile is required",
                message="profile is required for onboard",
            )

        command = [
            *self._build_base_command(),
            "onboard",
            "--non-interactive",
            "--accept-risk",
            "--auth-choice",
            auth_choice,
            f"--{auth_choice}",
            api_key,
        ]
        if self._gateway_port:
            command.extend(["--gateway-port", str(self._gateway_port)])
        if install_daemon:
            command.append("--install-daemon")
        if skip_channels:
            command.append("--skip-channels")
        if skip_search:
            command.append("--skip-search")
        if skip_hooks:
            command.append("--skip-hooks")
        if skip_health:
            command.append("--skip-health")

        # 当使用 custom-api-key 认证时，添加自定义参数
        if auth_choice == "custom-api-key":
            if custom_base_url:
                command.extend(["--custom-base-url", custom_base_url])
            if custom_model_id:
                command.extend(["--custom-model-id", custom_model_id])
            if custom_compatibility:
                command.extend(["--custom-compatibility", custom_compatibility])

        # 统一 openclaw workspace 到 witty workspace：写入 agents.defaults.workspace，
        # 运行时 config 优先于 env，是实际生效的杠杆。
        workspace = self._workspace_path()
        if workspace is not None:
            command.extend(["--workspace", str(workspace)])

        result = self._run_command(command)
        if result.returncode != 0:
            raise OpenClawOnboardError(
                command=command,
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
            )

    def _run_or_raise(
        self,
        *,
        action: str,
    ) -> None:
        result = self._invoke_gateway_command(action)
        if result.returncode == 0:
            return
        raise self._command_error(
            action=action,
            command=(
                tuple(result.args)
                if isinstance(result.args, Sequence)
                and not isinstance(result.args, str)
                else (str(result.args),)
            ),
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )

    def _invoke_gateway_command(
        self,
        action: str,
    ) -> CompletedProcess[str]:
        command = [*self._build_base_command(), "gateway", action]
        return self._run_command(command)

    def _run_command(self, command: list[str]) -> CompletedProcess[str]:
        try:
            return self._runner(command)
        except Exception as exc:  # pragma: no cover - defensive guard
            raise self._command_error(
                action=" ".join(command[:2]),
                command=command,
                returncode=-1,
                stdout="",
                stderr=str(exc),
            ) from exc

    def _command_error(
        self,
        *,
        action: str,
        command: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> OpenClawLifecycleError:
        if action == "status":
            return OpenClawGatewayStatusError(
                command=command,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
        if action == "stop":
            return OpenClawGatewayStopError(
                command=command,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
        if action == "mcp set":
            return OpenClawMcpSetError(
                command=command,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
        if action == "mcp unset":
            return OpenClawMcpUnsetError(
                command=command,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
        if action == "onboard":
            return OpenClawOnboardError(
                command=command,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
            )
        return OpenClawGatewayStartError(
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def _result_indicates_running(self, result: CompletedProcess[str]) -> bool:
        output = self._combined_output(result).lower()
        if "not running" in output or "stopped" in output or "inactive" in output:
            return False
        return "running" in output or "active" in output or "started" in output

    def _combined_output(self, result: CompletedProcess[str]) -> str:
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        return f"{stdout}\n{stderr}".strip()

    @staticmethod
    def _default_runner(command: list[str]) -> CompletedProcess[str]:
        return run(command, check=False, capture_output=True, text=True)
