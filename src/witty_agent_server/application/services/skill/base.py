from __future__ import annotations

import logging
import re
import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from witty_agent_server.application.services.skill.errors import (
    AgentSkillServiceError,
)
from witty_agent_server.application.services.skill.skill_client_port import (
    SkillClientPort,
)
from witty_service.config import get_settings


class AgentSkillServiceBase(ABC):
    runtime_type: str
    _ALLOWED_SOURCE_BASES: list[Path] = []

    def __init__(
        self,
        *,
        skill_client: SkillClientPort | None = None,
    ) -> None:
        self._skill_client = skill_client

    def _require_skill_client(self) -> SkillClientPort:
        """返回 skill_client，若子类未提供则抛出清晰错误。

        需要调用 skill_client 的子类应在 __init__中覆写并传入有效实现
        """
        if self._skill_client is None:
            raise RuntimeError(
                f"{type(self).__name__}: skill_client is required but was not "
                f"provided. Override __init__ and pass a SkillClientPort "
                f"implementation."
            )
        return self._skill_client

    @abstractmethod
    def list_skills(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """查询当前 runtime 可用的技能列表。"""

    @abstractmethod
    def install_skill(
        self,
        *,
        agent_id: str | None = None,
        skill_name: str,
        source_type: str | None = None,
        source_path: str | None = None,
        skill_source: str | None = None,
    ) -> dict[str, Any]:
        """安装技能到当前 runtime。source_path 非空时为本地技能目录。"""

    @abstractmethod
    def uninstall_skill(
        self,
        *,
        agent_id: str | None = None,
        skill_name: str,
        source_type: str | None = None,
        source_path: str | None = None,
        runtime_source: str | None = None,
    ) -> dict[str, Any]:
        """卸载当前 runtime 中的技能。"""

    @staticmethod
    def _is_local_path(path: str) -> bool:
        path = path.strip()
        return (
            path.startswith("/") 
            or path.startswith("~") 
            or path.startswith(".")
            or "\\" in path
            or ("/" in path and not path.startswith("http://") and not path.startswith("https://"))
        )

    @staticmethod
    def _remove_path(target: Path) -> None:
        """安全删除文件或目录。"""
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
            return
        if target.exists():
            shutil.rmtree(target)

    def _normalize_skill_name(
        self,
        skill_name: str,
        error_cls: type[AgentSkillServiceError],
    ) -> str:
        """校验并规范化 skill 名称，防止路径穿越。"""
        if not isinstance(skill_name, str) or not skill_name.strip():
            raise error_cls(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason="skill_name is empty",
            )
        normalized = skill_name.strip()
        if normalized in (".", ".."):
            raise error_cls(
                runtime_type=self.runtime_type,
                skill_name=normalized,
                reason="skill_name must not be '.' or '..'",
            )
        if re.search(r"[\\/]", normalized):
            raise error_cls(
                runtime_type=self.runtime_type,
                skill_name=normalized,
                reason="skill_name contains path separator",
            )
        return normalized

    @classmethod
    def _validate_source_path(cls, target: Path) -> Path:
        """校验 source_path 是否在允许的目录下，防止路径穿越。"""
        resolved = target.expanduser().resolve()
        bases = cls._ALLOWED_SOURCE_BASES or [
            get_settings().workspace.root_path() / "skill-repositories"
        ]
        for base in bases:
            base_resolved = base.resolve()
            try:
                resolved.relative_to(base_resolved)
                return resolved
            except ValueError:
                continue
        raise ValueError(
            f"Source path {resolved} is outside allowed directories: "
            f"{[str(b) for b in bases]}"
        )

    _logger = logging.getLogger(__name__)

    def _run_wittyhub_command(
        self,
        command: list[str],
        cwd: Path,
        *,
        skill_name: str,
        error_cls: type[AgentSkillServiceError],
        timeout: int | None = None,
        raise_on_error: bool = True,
    ) -> subprocess.CompletedProcess | None:
        """执行 wittyhub 命令并统一转换常见错误。

        子类通过此方法调用 wittyhub，避免重复的 try/except 样板代码。
        ``raise_on_error=False`` 时，CalledProcessError 仅记录警告而不抛出。
        """
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                cwd=cwd,
                timeout=timeout,
            )
            return result
        except subprocess.TimeoutExpired as exc:
            reason = f"wittyhub command timed out: {exc}"
            if not raise_on_error:
                self._logger.warning(
                    "wittyhub command timed out, runtime_type=%s skill_name=%s",
                    self.runtime_type,
                    skill_name,
                )
                return None
            raise error_cls(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=reason,
            ) from exc
        except FileNotFoundError:
            raise error_cls(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason="npx command not found",
            )
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            reason = stderr or stdout or f"wittyhub exited with code {exc.returncode}"
            if not raise_on_error:
                self._logger.warning(
                    "wittyhub command failed, runtime_type=%s skill_name=%s reason=%s",
                    self.runtime_type,
                    skill_name,
                    reason,
                )
                return None
            raise error_cls(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=reason,
            ) from exc
