import logging
import os
import shutil
from os.path import expanduser, normcase, normpath
from collections.abc import Callable, Sequence
from subprocess import CompletedProcess, run
from pathlib import Path

from witty_agent_server.application.composition.models import RuntimeInstance
from witty_agent_server.application.materialization.openclaw_env import (
    build_openclaw_env,
)


CommandRunner = Callable[[list[str]], CompletedProcess[str]]
logger = logging.getLogger(__name__)


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


class OpenClawGatewayInstallError(OpenClawLifecycleError):
    def __init__(
        self,
        *,
        command: Sequence[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        super().__init__(
            action="install",
            command=command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            message="openclaw gateway install failed",
        )


class OpenClawLifecycleService:
    def __init__(self, runner: CommandRunner | None = None) -> None:
        self._runner: CommandRunner = runner or self._default_runner

    def probe_instance(self, instance: RuntimeInstance) -> bool:
        status_result = self._invoke_gateway_command("status", instance=instance)
        if not self._result_indicates_running(status_result):
            return False
        if not self._status_belongs_to_instance(status_result, instance):
            logger.warning(
                "openclaw status mismatch instance binding: agent_id=%s profile=%s output=%s",
                instance.agent_id,
                instance.profile_name,
                self._combined_output(status_result),
            )
            return False
        return True

    def setup_instance(self, instance: RuntimeInstance) -> None:
        self._run_or_raise(action="setup", instance=instance)

    def stop_instance(self, instance: RuntimeInstance) -> None:
        self._run_or_raise(action="stop", instance=instance)

    def install_instance(self, instance: RuntimeInstance, *, force: bool = False) -> None:
        self._run_or_raise(action="install", instance=instance, force=force)

    def start_instance(self, instance: RuntimeInstance) -> None:
        self._run_or_raise(action="start", instance=instance)

    def backup_instance(self, instance: RuntimeInstance) -> None:
        """将当前 profile home 备份到 agent 工作区，供后续恢复使用。"""
        state_dir = instance.state_dir
        runtime_backup_dir = instance.runtime_backup_dir
        if not isinstance(state_dir, Path) or not isinstance(runtime_backup_dir, Path):
            logger.warning(
                "skip openclaw backup because state_dir or runtime_backup_dir is missing: agent_id=%s profile=%s state_dir=%s runtime_backup_dir=%s",
                instance.agent_id,
                instance.profile_name,
                state_dir,
                runtime_backup_dir,
            )
            return
        if not state_dir.exists():
            logger.warning(
                "skip openclaw backup because state_dir does not exist: agent_id=%s profile=%s state_dir=%s",
                instance.agent_id,
                instance.profile_name,
                state_dir,
            )
            return
        runtime_backup_dir.parent.mkdir(parents=True, exist_ok=True)
        if runtime_backup_dir.exists():
            shutil.rmtree(runtime_backup_dir)
        shutil.copytree(state_dir, runtime_backup_dir)
        logger.info(
            "backed up openclaw profile home: agent_id=%s profile=%s state_dir=%s backup_dir=%s",
            instance.agent_id,
            instance.profile_name,
            state_dir,
            runtime_backup_dir,
        )

    def _run_or_raise(
        self,
        *,
        action: str,
        instance: RuntimeInstance,
        force: bool = False,
    ) -> None:
        result = self._invoke_gateway_command(action, instance=instance, force=force)
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
        *,
        instance: RuntimeInstance,
        force: bool = False,
    ) -> CompletedProcess[str]:
        command = self._build_command(action=action, instance=instance, force=force)
        try:
            env = (
                build_openclaw_env(
                    state_dir=instance.state_dir,
                    base_env=os.environ,
                )
                if isinstance(instance.state_dir, Path)
                else os.environ.copy()
            )
            logger.info(
                "invoke openclaw lifecycle command: action=%s agent_id=%s profile=%s port=%s state_dir=%s command=%s",
                action,
                instance.agent_id,
                instance.profile_name,
                instance.port,
                instance.state_dir,
                command,
            )
            return self._runner(command, env=env)
        except Exception as exc:  # pragma: no cover - defensive guard
            raise self._command_error(
                action=action,
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
        if action == "setup":
            return OpenClawLifecycleError(
                action="setup",
                command=command,
                returncode=returncode,
                stdout=stdout,
                stderr=stderr,
                message="openclaw setup failed",
            )
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
        if action == "install":
            return OpenClawGatewayInstallError(
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
        if (
            "not running" in output
            or "stopped" in output
            or "inactive" in output
            or "disabled" in output
        ):
            return False
        if (
            "running" in output
            or "active" in output
            or "started" in output
            or "enabled" in output
        ):
            return True
        if result.returncode != 0:
            return False
        return False

    def _status_belongs_to_instance(
        self,
        result: CompletedProcess[str],
        instance: RuntimeInstance,
    ) -> bool:
        output = self._combined_output(result)
        if not output:
            return False
        output_lower = output.lower()
        profile_name = instance.profile_name or ""
        if profile_name and profile_name.lower() in output_lower:
            return True
        if instance.config_path is None:
            return True
        expected_paths = {
            self._normalize_path(str(instance.config_path)),
            self._normalize_path(str(instance.config_path.expanduser())),
        }
        for candidate in self._extract_config_paths(output):
            if self._normalize_path(candidate) in expected_paths:
                return True
            expanded_candidate = self._normalize_path(expanduser(candidate))
            if expanded_candidate in expected_paths:
                return True
        return False

    def _extract_config_paths(self, output: str) -> list[str]:
        config_paths: list[str] = []
        for line in output.splitlines():
            line_lower = line.lower()
            if not line_lower.startswith("config "):
                continue
            if ":" not in line:
                continue
            _, _, raw_value = line.partition(":")
            value = raw_value.strip()
            if value:
                config_paths.append(value)
        return config_paths

    def _normalize_path(self, path_value: str) -> str:
        return normcase(normpath(path_value.strip()))

    def _combined_output(self, result: CompletedProcess[str]) -> str:
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        return f"{stdout}\n{stderr}".strip()

    def _build_command(
        self,
        *,
        action: str,
        instance: RuntimeInstance,
        force: bool = False,
    ) -> list[str]:
        """构造实例级 openclaw lifecycle 命令。"""
        profile_name = instance.profile_name
        if not isinstance(profile_name, str) or not profile_name:
            raise ValueError("openclaw runtime instance requires profile_name")
        command = ["openclaw", "--profile", profile_name]
        if action == "setup":
            workspace_dir = self._resolve_setup_workspace(instance)
            return [*command, "setup", "--workspace", workspace_dir]
        if action == "status":
            return [*command, "gateway", "status", "--require-rpc"]
        if action == "stop":
            return [*command, "gateway", "stop"]
        if action == "install":
            if not isinstance(instance.port, int):
                raise ValueError("openclaw runtime instance requires port")
            install_command = [*command, "gateway", "install", "--port", str(instance.port)]
            if force:
                install_command.append("--force")
            return install_command
        if action == "start":
            return [*command, "gateway", "start"]
        raise ValueError(f"unsupported lifecycle action: {action}")

    def _resolve_setup_workspace(self, instance: RuntimeInstance) -> str:
        """解析 setup 阶段显式传入的 workspace 根目录。"""
        if isinstance(instance.state_dir, Path):
            return str(instance.state_dir)
        raise ValueError("openclaw setup requires state_dir for explicit workspace")

    @staticmethod
    def _default_runner(
        command: list[str], *, env: dict[str, str] | None = None
    ) -> CompletedProcess[str]:
        return run(
            command,
            check=False,
            capture_output=True,
            text=True,
            env=env,
        )
