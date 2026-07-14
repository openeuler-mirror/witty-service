from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from witty_agent_server.application.services.skill.base import AgentSkillServiceBase
from witty_agent_server.application.services.skill.errors import (
    OpenClawSkillNotRemovableError,
    OpenClawSkillsInstallError,
    OpenClawSkillsQueryError,
    OpenClawSkillsUninstallError,
)
from witty_agent_server.application.services.skill.openclaw_skill_client import (
    OpenClawSkillClient,
)
from witty_agent_server.application.services.skill.skill_client_port import (
    SkillClientPort,
)
from witty_agent_server.infra.ws.openclaw_gateway_client import (
    OpenClawGatewayClientError,
)
from witty_service.config import get_settings


logger = logging.getLogger(__name__)


class OpenClawSkillService(AgentSkillServiceBase):
    runtime_type = "openclaw"
    skills_dir = Path.home() / ".openclaw" / "skills"

    def __init__(
        self,
        *,
        skill_client: SkillClientPort | None = None,
    ) -> None:
        super().__init__(skill_client=skill_client or OpenClawSkillClient())

    @classmethod
    def _get_workspace_skills_dir(cls, agent_id: str | None) -> Path:
        """获取 agent 专属的技能安装目录"""
        if agent_id:
            return Path.home() / ".openclaw" / f"workspace-{agent_id}" / "skills"
        return cls.skills_dir

    @classmethod
    def _get_workspace_root(cls, agent_id: str | None) -> Path:
        if agent_id:
            return Path.home() / ".openclaw" / f"workspace-{agent_id}"
        return cls.skills_dir.parent

    @classmethod
    def _get_profile_root(cls, agent_id: str | None) -> Path:
        if agent_id:
            return Path.home() / f".openclaw-{agent_id}"
        return Path.home() / ".openclaw"

    @classmethod
    def _get_profile_plugin_skills_dir(cls, agent_id: str | None) -> Path:
        return cls._get_profile_root(agent_id) / "plugin-skills"

    @classmethod
    def _get_personal_skills_dir(cls) -> Path:
        return Path.home() / ".agents" / "skills"

    @classmethod
    def _build_allowed_delete_bases(
        cls,
        agent_id: str | None,
        runtime_source: str | None = None,
    ) -> list[Path]:
        """按运行时来源限制可删除目录，避免误删其他系统路径。"""
        if runtime_source == "agents-skills-personal":
            return [cls._get_personal_skills_dir()]
        if runtime_source == "openclaw-extra":
            return [cls._get_profile_plugin_skills_dir(agent_id)] if agent_id else []
        if agent_id:
            return [cls._get_workspace_skills_dir(agent_id)]
        return []

    _ALLOWED_SOURCE_BASES: list[Path] = [
        get_settings().workspace.root_path() / "skill-repositories",
    ]

    @classmethod
    def _validate_path_under_allowed_bases(
        cls,
        target: Path,
        agent_id: str | None = None,
        runtime_source: str | None = None,
    ) -> Path:
        resolved = cls._normalize_delete_target(target)
        allowed_bases = cls._build_allowed_delete_bases(agent_id, runtime_source=runtime_source)
        for base in allowed_bases:
            base_resolved = cls._normalize_delete_target(base)
            try:
                resolved.relative_to(base_resolved)
            except ValueError:
                continue
            current = base_resolved
            for part in resolved.relative_to(base_resolved).parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise ValueError(
                        f"Path {resolved} traverses symbolic link outside allowed directories"
                    )
            return resolved
        raise ValueError(
            f"Path {resolved} is outside allowed directories: "
            f"{[str(b) for b in allowed_bases]}"
        )

    @classmethod
    def _validate_source_path_under_workspace(cls, target: Path) -> Path:
        resolved = target.expanduser().resolve()
        for base in cls._ALLOWED_SOURCE_BASES:
            base_resolved = base.resolve()
            try:
                resolved.relative_to(base_resolved)
                return resolved
            except ValueError:
                continue
        raise ValueError(
            f"Source path {resolved} is outside allowed directories: "
            f"{[str(b) for b in cls._ALLOWED_SOURCE_BASES]}"
        )

    def list_skills(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """查询并返回当前 agent 可用的技能摘要列表。"""
        logger.info(
            "list_skills requested, runtime_type=%s agent_id=%s",
            self.runtime_type,
            agent_id,
        )
        try:
            skills_payload = self._require_skill_client().get_skills_status(agent_id=agent_id)
        except OpenClawGatewayClientError as exc:
            logger.exception(
                "list_skills openclaw rpc failed, runtime_type=%s agent_id=%s code=%s",
                self.runtime_type,
                agent_id,
                exc.code,
            )
            raise OpenClawSkillsQueryError(
                runtime_type=self.runtime_type,
                code=exc.code,
                message=exc.message,
            ) from exc

        logger.info(
            "list_skills success, runtime_type=%s agent_id=%s skill_count=%s",
            self.runtime_type,
            agent_id,
            self._count_eligible_skills(skills_payload),
        )
        return {
            "runtime_type": self.runtime_type,
            "skills": self._normalize_eligible_skills(skills_payload),
        }

    def _normalize_eligible_skills(
        self, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """筛选可用技能，并裁剪为对外暴露的固定字段。"""
        for key in ("skills", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [
                    self._build_skill_summary(item)
                    for item in value
                    if isinstance(item, dict) and item.get("eligible") is True
                ]
        if payload and all(isinstance(key, str) for key in payload):
            return [
                self._build_skill_summary({"name": key, "description": value})
                for key, value in payload.items()
            ]
        return []

    def _count_eligible_skills(self, payload: dict[str, Any]) -> int:
        """统计当前响应中可用技能数量。"""
        return len(self._normalize_eligible_skills(payload))

    def _build_skill_summary(self, item: dict[str, Any]) -> dict[str, Any]:
        """构造对外返回的技能摘要，仅保留约定字段。"""
        return {
            "name": item.get("name"),
            "description": item.get("description"),
            "filePath": item.get("filePath"),
            "source": item.get("source"),
        }

    def install_skill(
        self,
        *,
        agent_id: str | None = None,
        skill_name: str,
        source_type: str | None = None,
        source_path: str | None = None,
        skill_source: str | None = None,
    ) -> dict[str, Any]:
        if source_type == "wittyhub":
            return self._install_wittyhub_skill(
                agent_id=agent_id,
                skill_name=skill_name,
                skill_source=skill_source,
            )

        install_target = source_path if source_path else skill_name
        is_local = self._is_local_path(install_target)
        
        command = ["openclaw", "skills", "install", install_target]
        
        if agent_id:
            command.extend(["--profile", agent_id])
        
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                cwd=Path.home(),
            )
            
            logger.info(
                "install_skill success, runtime_type=%s agent_id=%s target=%s stdout=%s",
                self.runtime_type,
                agent_id,
                install_target,
                result.stdout.strip(),
            )
            
            return {
                "runtime_type": self.runtime_type,
                "skill_name": skill_name,
                "installed": True,
                "install_channel": "local" if is_local else "clawhub",
                "stdout": result.stdout.strip(),
                "filePath": self._get_installed_skill_file_path(
                    agent_id=agent_id,
                    skill_name=skill_name,
                ),
            }
        
        except FileNotFoundError as exc:
            raise OpenClawSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason="openclaw command not found",
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            reason = stderr or stdout or f"openclaw exited with code {exc.returncode}"
            raise OpenClawSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=reason,
            ) from exc
        except Exception as exc:
            raise OpenClawSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=str(exc),
            ) from exc
    
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

    def _install_local_skill(self, skill_name: str, source_path: str) -> dict[str, Any]:
        src = Path(source_path).expanduser().resolve()
        try:
            src = self._validate_source_path_under_workspace(src)
        except ValueError as exc:
            raise OpenClawSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=str(exc),
            ) from exc
        if not src.exists():
            raise OpenClawSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=f"source path does not exist: {src}",
            )

        self.skills_dir.mkdir(parents=True, exist_ok=True)
        dst = self.skills_dir / skill_name

        try:
            dst = self._validate_path_under_allowed_bases(dst)
        except ValueError as exc:
            raise OpenClawSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=str(exc),
            ) from exc

        if dst.exists():
            shutil.rmtree(dst)

        if src.is_file():
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst / src.name)
        else:
            shutil.copytree(src, dst)

        logger.info(
            "install_local_skill success, runtime_type=%s skill_name=%s src=%s dst=%s",
            self.runtime_type,
            skill_name,
            src,
            dst,
        )
        return {
            "runtime_type": self.runtime_type,
            "skill_name": skill_name,
            "installed": True,
            "install_channel": "local_copy",
        }

    def _install_wittyhub_skill(
        self,
        *,
        agent_id: str | None,
        skill_name: str,
        skill_source: str | None,
    ) -> dict[str, Any]:
        normalized_name = self._normalize_skill_name(
            skill_name=skill_name,
            error_cls=OpenClawSkillsInstallError,
        )
        normalized_skill_source = (skill_source or "").strip()
        logger.info("normalized_skill_source=%s", normalized_skill_source)
        if not normalized_skill_source:
            raise OpenClawSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=normalized_name,
                reason="skill_source is required for wittyhub install",
            )

        workspace_root = self._get_workspace_root(agent_id)
        workspace_root.mkdir(parents=True, exist_ok=True)
        command = [
            "npx",
            "wittyhub",
            "add",
            normalized_skill_source,
            "--skill",
            normalized_name,
            "--agent",
            "openclaw",
            "-y",
        ]

        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                cwd=workspace_root,
            )
            logger.info(
                (
                    "install_wittyhub_skill success, runtime_type=%s agent_id=%s "
                    "skill_name=%s skill_source=%s cwd=%s stdout=%s"
                ),
                self.runtime_type,
                agent_id,
                normalized_name,
                normalized_skill_source,
                workspace_root,
                result.stdout.strip(),
            )
            return {
                "runtime_type": self.runtime_type,
                "skill_name": normalized_name,
                "installed": True,
                "install_channel": "wittyhub",
                "stdout": result.stdout.strip(),
                "filePath": self._get_installed_skill_file_path(
                    agent_id=agent_id,
                    skill_name=normalized_name,
                ),
            }
        except FileNotFoundError as exc:
            raise OpenClawSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=normalized_name,
                reason="npx command not found",
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            reason = stderr or stdout or f"wittyhub exited with code {exc.returncode}"
            raise OpenClawSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=normalized_name,
                reason=reason,
            ) from exc
        except Exception as exc:
            raise OpenClawSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=normalized_name,
                reason=str(exc),
            ) from exc

    def _get_installed_skill_file_path(
        self,
        *,
        agent_id: str | None,
        skill_name: str,
    ) -> str | None:
        """Read the installed path without turning a successful install into a failure."""
        try:
            payload = self._require_skill_client().get_skills_status(agent_id=agent_id)
            normalized_name = skill_name.strip()
            for item in self._normalize_eligible_skills(payload):
                if item.get("name") != normalized_name:
                    continue
                file_path = item.get("filePath")
                return file_path.strip() if isinstance(file_path, str) and file_path.strip() else None
        except Exception:
            logger.warning(
                "Failed to resolve installed skill path: runtime_type=%s agent_id=%s skill_name=%s",
                self.runtime_type,
                agent_id,
                skill_name,
                exc_info=True,
            )
        expected_file = self._get_workspace_skills_dir(agent_id) / skill_name.strip() / "SKILL.md"
        if expected_file.is_file():
            return str(expected_file)
        return None

    def _install_skill_via_clawhub(self, skill_name: str) -> None:
        command = ["clawhub", "install", skill_name, "--force"]
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise OpenClawSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason="clawhub command not found",
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            reason = stderr or stdout or f"clawhub exited with code {exc.returncode}"
            raise OpenClawSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=reason,
            ) from exc
        except Exception as exc:  # pragma: no cover - fs/environment specific
            raise OpenClawSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=str(exc),
            ) from exc

    def uninstall_skill(
        self,
        *,
        agent_id: str | None = None,
        skill_name: str,
        source_type: str | None = None,
        source_path: str | None = None,
        runtime_source: str | None = None,
    ) -> dict[str, Any]:
        normalized_name = self._normalize_skill_name(
            skill_name=skill_name,
            error_cls=OpenClawSkillsUninstallError,
        )

        if source_type == "builtin":
            if not source_path:
                raise OpenClawSkillsUninstallError(
                    runtime_type=self.runtime_type,
                    skill_name=normalized_name,
                    reason="source_path is required for runtime-discovered skill uninstall",
                )
            return self._uninstall_runtime_discovered_skill(
                normalized_name,
                source_path,
                agent_id=agent_id,
                runtime_source=runtime_source,
            )
        if source_type == "wittyhub":
            return self._uninstall_wittyhub_skill(normalized_name, agent_id=agent_id)

        return self._uninstall_local_skill(normalized_name, agent_id=agent_id)

    def _uninstall_runtime_discovered_skill(
        self,
        skill_name: str,
        source_path: str,
        *,
        agent_id: str | None = None,
        runtime_source: str | None = None,
    ) -> dict[str, Any]:
        resolved_runtime_source = self._resolve_runtime_source(
            Path(source_path),
            agent_id=agent_id,
            runtime_source=runtime_source,
        )
        if resolved_runtime_source == "openclaw-bundled":
            raise OpenClawSkillNotRemovableError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason="bundled skill cannot be uninstalled",
            )

        uninstall_channel = {
            "agents-skills-personal": "runtime_personal_remove",
            "openclaw-extra": "runtime_extra_remove",
            "openclaw-workspace": "runtime_workspace_remove",
        }.get(resolved_runtime_source, "builtin_remove")
        return self._uninstall_builtin_skill(
            skill_name,
            source_path,
            agent_id=agent_id,
            runtime_source=resolved_runtime_source,
            uninstall_channel=uninstall_channel,
        )

    def _uninstall_wittyhub_skill(
        self,
        skill_name: str,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        workspace_root = self._get_workspace_root(agent_id)
        workspace_root.mkdir(parents=True, exist_ok=True)
        command = [
            "npx",
            "wittyhub",
            "remove",
            skill_name,
            "--agent",
            "openclaw",
            "-y",
        ]
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                cwd=workspace_root,
            )
            logger.info(
                (
                    "uninstall_wittyhub_skill success, runtime_type=%s agent_id=%s "
                    "skill_name=%s cwd=%s stdout=%s stderr=%s"
                ),
                self.runtime_type,
                agent_id,
                skill_name,
                workspace_root,
                result.stdout.strip(),
                result.stderr.strip(),
            )
            return {
                "runtime_type": self.runtime_type,
                "skill_name": skill_name,
                "uninstalled": True,
                "uninstall_channel": "wittyhub",
            }
        except FileNotFoundError as exc:
            raise OpenClawSkillsUninstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason="npx command not found",
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            reason = stderr or stdout or f"wittyhub exited with code {exc.returncode}"
            raise OpenClawSkillsUninstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=reason,
            ) from exc
        except Exception as exc:
            raise OpenClawSkillsUninstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=str(exc),
            ) from exc

    def _uninstall_skill_via_clawhub(self, skill_name: str) -> None:
        command = ["clawhub", "uninstall", skill_name, "--yes"]
        try:
            result = subprocess.run(command, check=True, capture_output=True, text=True)
            logger.info(
                "clawhub uninstall success, skill_name=%s command=%s stdout=%s stderr=%s",
                skill_name, command, result.stdout.strip(), result.stderr.strip(),
            )
        except FileNotFoundError as exc:
            raise OpenClawSkillsUninstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason="clawhub command not found",
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            stdout = (exc.stdout or "").strip()
            reason = stderr or stdout or f"clawhub exited with code {exc.returncode}"
            raise OpenClawSkillsUninstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=reason,
            ) from exc
        except Exception as exc:
            raise OpenClawSkillsUninstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=str(exc),
            ) from exc

    def _uninstall_local_skill(self, skill_name: str, agent_id: str | None = None) -> dict[str, Any]:
        skills_dir = self._get_workspace_skills_dir(agent_id)
        dst = skills_dir / skill_name
        
        try:
            dst = self._validate_path_under_allowed_bases(dst, agent_id=agent_id)
        except ValueError as exc:
            raise OpenClawSkillsUninstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=str(exc),
            ) from exc
        
        self._remove_installed_path(dst)

        logger.info(
            "uninstall_local_skill success, runtime_type=%s agent_id=%s skill_name=%s dst=%s",
            self.runtime_type,
            agent_id,
            skill_name,
            dst,
        )
        return {
            "runtime_type": self.runtime_type,
            "skill_name": skill_name,
            "uninstalled": True,
            "uninstall_channel": "local_remove",
        }

    @classmethod
    def _resolve_runtime_source(
        cls,
        source_path: Path,
        *,
        agent_id: str | None,
        runtime_source: str | None,
    ) -> str | None:
        if runtime_source:
            return runtime_source

        resolved = cls._normalize_delete_target(source_path)
        candidates: tuple[tuple[str, list[Path]], ...] = (
            ("openclaw-workspace", cls._build_allowed_delete_bases(agent_id, "openclaw-workspace")),
            ("openclaw-extra", cls._build_allowed_delete_bases(agent_id, "openclaw-extra")),
            ("agents-skills-personal", cls._build_allowed_delete_bases(agent_id, "agents-skills-personal")),
        )
        for source_name, bases in candidates:
            for base in bases:
                try:
                    resolved.relative_to(cls._normalize_delete_target(base))
                    return source_name
                except ValueError:
                    continue
        return None

    def _uninstall_builtin_skill(
        self,
        skill_name: str,
        source_path: str,
        agent_id: str | None = None,
        runtime_source: str | None = None,
        uninstall_channel: str = "builtin_remove",
    ) -> dict[str, Any]:
        try:
            dst = self._validate_path_under_allowed_bases(
                Path(source_path),
                agent_id=agent_id,
                runtime_source=runtime_source,
            )
        except ValueError as exc:
            raise OpenClawSkillsUninstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=str(exc),
            ) from exc
        self._remove_installed_path(dst)

        logger.info(
            (
                "uninstall_builtin_skill success, runtime_type=%s skill_name=%s "
                "runtime_source=%s dst=%s"
            ),
            self.runtime_type,
            skill_name,
            runtime_source,
            dst,
        )
        return {
            "runtime_type": self.runtime_type,
            "skill_name": skill_name,
            "uninstalled": True,
            "uninstall_channel": uninstall_channel,
        }

    @staticmethod
    def _normalize_delete_target(target: Path) -> Path:
        candidate = target.expanduser()
        if candidate.name.lower() == "skill.md":
            candidate = candidate.parent
        return Path(os.path.abspath(str(candidate)))

    @staticmethod
    def _remove_installed_path(target: Path) -> None:
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
            return
        if target.exists():
            shutil.rmtree(target)

    def _normalize_skill_name(
        self,
        *,
        skill_name: str,
        error_cls: type[OpenClawSkillsInstallError] | type[OpenClawSkillsUninstallError],
    ) -> str:
        if not isinstance(skill_name, str) or not skill_name.strip():
            raise error_cls(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason="skill_name is empty",
            )
        normalized_name = skill_name.strip()
        if re.search(r"[\\/]", normalized_name):
            raise error_cls(
                runtime_type=self.runtime_type,
                skill_name=normalized_name,
                reason="skill_name contains path separator",
            )
        return normalized_name
