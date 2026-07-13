from collections.abc import Callable, Sequence
import json
import logging
import subprocess
import threading
import time
from subprocess import CompletedProcess, Popen, run

from witty_agent_server.application.services.agent._process_utils import (
    _KILL_TIMEOUT_SECONDS,
    _STDERR_DRAIN_JOIN_TIMEOUT,
    _STOP_TIMEOUT_SECONDS,
    port_is_listening,
    start_stderr_drainer,
)


logger = logging.getLogger(__name__)


CommandRunner = Callable[[list[str]], CompletedProcess[str]]


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

    def probe_running(self) -> bool:
        if not self._profile:
            return False
        result = self._invoke_gateway_command("status")
        if result.returncode != 0:
            return False
        return self._result_indicates_running(result)

    def stop(self) -> None:
        """停止 openclaw gateway 并执行完整卸载"""
        if not self._profile:
            return
        
        # 构建卸载命令
        command = self._build_base_command() + [
            "uninstall",
            "--yes",
            "--non-interactive",
            "--all"
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

        command = self._build_base_command() + [
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
        )

        self._stderr_drainer = start_stderr_drainer(
            self._gateway_process,
            logger=logger,
            log_prefix="openclaw gateway stderr",
            thread_name="openclaw-gateway-stderr-drainer",
        )

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
                logger.info("Gateway started successfully on port %s", self._gateway_port)
                return
            time.sleep(1)

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
        if process is None:
            return
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

        drainer = self._stderr_drainer
        self._stderr_drainer = None
        if drainer is not None and drainer.is_alive():
            drainer.join(timeout=_STDERR_DRAIN_JOIN_TIMEOUT)

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
        command = self._build_base_command() + ["mcp", "set", name, config_str]
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

        command = self._build_base_command() + ["mcp", "unset", name]
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

        command = self._build_base_command() + [
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
        command = self._build_base_command() + ["gateway", action]
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
        if "running" in output or "active" in output or "started" in output:
            return True
        return False

    def _combined_output(self, result: CompletedProcess[str]) -> str:
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        return f"{stdout}\n{stderr}".strip()

    @staticmethod
    def _default_runner(command: list[str]) -> CompletedProcess[str]:
        return run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
