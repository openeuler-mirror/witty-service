from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

from witty_agent_server.application.services.skill.base import AgentSkillServiceBase
from witty_agent_server.application.services.skill.errors import (
    OpenCodeSkillsInstallError,
    OpenCodeSkillsQueryError,
    OpenCodeSkillsUninstallError,
)
from witty_service.config import get_settings


logger = logging.getLogger(__name__)


class OpenCodeSkillService(AgentSkillServiceBase):
    runtime_type = "opencode"

    @classmethod
    def _get_xdg_config_home(cls, agent_id: str | None) -> Path:
        """返回 opencode 的 XDG_CONFIG_HOME 目录。"""
        workspace_root = get_settings().workspace.root_path()
        if agent_id:
            return workspace_root / "agent-workspaces" / agent_id / "workspace"
        return workspace_root / "agent-workspaces" / "_default" / "workspace"

    @classmethod
    def _get_skills_dir(cls, agent_id: str | None) -> Path:
        """返回技能统一存放目录。"""
        return cls._get_xdg_config_home(agent_id) / ".agents" / "skills"

    @staticmethod
    def _parse_skill_md(skill_md_path: Path) -> dict[str, str]:
        """解析 SKILL.md 的 YAML frontmatter, 提取 name 和 description。"""
        if not skill_md_path.is_file():
            return {}
        try:
            text = skill_md_path.read_text(encoding="utf-8")
        except OSError:
            return {}

        stripped = text.lstrip()
        if not stripped.startswith("---"):
            return {}

        parts = stripped.split("---", maxsplit=2)
        if len(parts) < 3:
            return {}

        frontmatter = parts[1]
        result: dict[str, str] = {}
        for line in frontmatter.splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            match = re.match(r"^(\w+)\s*:\s*(.+)$", line)
            if not match:
                continue
            key = match.group(1)
            value = match.group(2).strip().strip("\"'")
            if key in ("name", "description"):
                result[key] = value

        return result

    @classmethod
    def _validate_path_under_skills_dir(
        cls,
        target: Path,
        agent_id: str | None = None,
    ) -> Path:
        """校验 target 在 skills 目录下，拒绝符号链接。"""
        skills_dir = cls._get_skills_dir(agent_id).resolve()
        resolved = target.expanduser().resolve()

        try:
            resolved.relative_to(skills_dir)
        except ValueError:
            raise ValueError(
                f"Path {resolved} is outside allowed skills directory {skills_dir}"
            )

        if resolved.is_symlink():
            raise ValueError(
                f"Path {resolved} is a symbolic link, which is not allowed"
            )
        return resolved

    def list_skills(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """查询并返回当前 agent 可用的技能列表。

        遍历 .agents/skills/ 目录，解析每个子目录下的 SKILL.md 获取 name 和 description。
        """
        logger.info(
            "list_skills requested, runtime_type=%s agent_id=%s",
            self.runtime_type,
            agent_id,
        )

        skills_dir = self._get_skills_dir(agent_id)
        discovered: list[dict[str, Any]] = []
        if not skills_dir.is_dir():
            logger.info(
                "list_skills: skills dir not found, returning empty list. path=%s",
                skills_dir,
            )
            return {
                "runtime_type": self.runtime_type,
                "skills": [],
            }

        try:
            for entry in sorted(skills_dir.iterdir()):
                if not entry.is_dir():
                    continue
                skill_md = entry / "SKILL.md"
                metadata = self._parse_skill_md(skill_md)
                skill_name = metadata.get("name") or entry.name
                discovered.append({
                    "name": skill_name,
                    "description": metadata.get("description"),
                    "filePath": str(skill_md) if skill_md.is_file() else None,
                    "source": "opencode-filesystem",
                })
        except OSError as exc:
            logger.exception(
                "list_skills failed to iterate skills dir, path=%s",
                skills_dir,
            )
            raise OpenCodeSkillsQueryError(
                runtime_type=self.runtime_type,
                code="SKILLS_DIR_READ_FAILED",
                message=str(exc),
            ) from exc

        logger.info(
            "list_skills success, runtime_type=%s agent_id=%s skill_count=%s",
            self.runtime_type,
            agent_id,
            len(discovered),
        )
        return {
            "runtime_type": self.runtime_type,
            "skills": discovered,
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
        """安装技能到 opencode runtime。

        支持两种安装渠道:
        - 本地路径: 复制到 skills 目录
        - 其他: 统一通过 npx wittyhub 安装
        """
        install_target = source_path if source_path else skill_name
        is_local = self._is_local_path(install_target)

        if is_local:
            return self._install_local_skill(
                agent_id=agent_id,
                skill_name=skill_name,
                source_path=source_path or skill_name,
            )

        # 非本地路径统一走 wittyhub 安装
        return self._install_wittyhub_skill(
            agent_id=agent_id,
            skill_name=skill_name,
            skill_source=skill_source,
        )

    def _install_local_skill(
        self,
        *,
        agent_id: str | None,
        skill_name: str,
        source_path: str,
    ) -> dict[str, Any]:
        """安装本地技能：将源目录/文件复制到 skills 目录下。"""
        normalized_name = self._normalize_skill_name(
            skill_name=skill_name,
            error_cls=OpenCodeSkillsInstallError,
            )

        src = Path(source_path).expanduser().resolve()
        try:
            src = self._validate_source_path(src)
        except ValueError as exc:
            raise OpenCodeSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=normalized_name,
                reason=str(exc),
            ) from exc

        if not src.exists():
            raise OpenCodeSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=normalized_name,
                reason=f"source path does not exist: {src}",
            )

        skills_dir = self._get_skills_dir(agent_id)
        skills_dir.mkdir(parents=True, exist_ok=True)
        dst = skills_dir / normalized_name

        try:
            dst = self._validate_path_under_skills_dir(dst, agent_id=agent_id)
        except ValueError as exc:
            raise OpenCodeSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=normalized_name,
                reason=str(exc),
            ) from exc

        if dst.exists():
            shutil.rmtree(dst)

        if src.is_file():
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst / src.name)
        else:
            shutil.copytree(src, dst)

        skill_md = dst / "SKILL.md"
        logger.info(
            "install_local_skill success, runtime_type=%s agent_id=%s "
            "skill_name=%s src=%s dst=%s",
            self.runtime_type,
            agent_id,
            normalized_name,
            src,
            dst,
        )
        return {
            "runtime_type": self.runtime_type,
            "skill_name": normalized_name,
            "installed": True,
            "install_channel": "local_copy",
            "filePath": str(skill_md) if skill_md.is_file() else None,
        }

    def _install_wittyhub_skill(
        self,
        *,
        agent_id: str | None,
        skill_name: str,
        skill_source: str | None,
    ) -> dict[str, Any]:
        """通过 wittyhub 安装技能到 .agents/skills/ 目录。"""
        normalized_name = self._normalize_skill_name(
            skill_name=skill_name,
            error_cls=OpenCodeSkillsInstallError,
            )
        normalized_skill_source = (skill_source or "").strip()
        if not normalized_skill_source:
            raise OpenCodeSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=normalized_name,
                reason="skill_source is required for wittyhub install",
            )

        xdg_config_home = self._get_xdg_config_home(agent_id)
        xdg_config_home.mkdir(parents=True, exist_ok=True)

        try:
            result = self._run_wittyhub_command(
                [
                    "npx",
                    "wittyhub",
                    "add",
                    normalized_skill_source,
                    "--skill",
                    normalized_name,
                    "--agent",
                    "opencode",
                    "-y",
                ],
                cwd=xdg_config_home,
                skill_name=normalized_name,
                error_cls=OpenCodeSkillsInstallError,
                timeout=30,
            )
            logger.info(
                "install_wittyhub_skill success, runtime_type=%s agent_id=%s "
                "skill_name=%s skill_source=%s cwd=%s stdout=%s",
                self.runtime_type,
                agent_id,
                normalized_name,
                normalized_skill_source,
                xdg_config_home,
                result.stdout.strip(),
            )

            # 验证 wittyhub 安装到了 .agents/skills/<normalized_name>/
            installed_dir = self._get_skills_dir(agent_id) / normalized_name
            skill_md = installed_dir / "SKILL.md"
            if not skill_md.is_file():
                raise OpenCodeSkillsInstallError(
                    runtime_type=self.runtime_type,
                    skill_name=normalized_name,
                    reason=(
                        f"skill not found after wittyhub add at {installed_dir}; "
                        "expected .agents/skills/<name>/SKILL.md"
                    ),
                )

            return {
                "runtime_type": self.runtime_type,
                "skill_name": normalized_name,
                "installed": True,
                "install_channel": "wittyhub",
                "filePath": str(skill_md),
            }
        except OpenCodeSkillsInstallError:
            raise
        except Exception as exc:
            raise OpenCodeSkillsInstallError(
                runtime_type=self.runtime_type,
                skill_name=normalized_name,
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
        """卸载 opencode runtime 中的技能。"""
        normalized_name = self._normalize_skill_name(
            skill_name=skill_name,
            error_cls=OpenCodeSkillsUninstallError,
            )

        # 本地路径安装的技能走本地卸载
        if source_path and self._is_local_path(source_path):
            return self._uninstall_local_skill(normalized_name, agent_id=agent_id)

        # 其他情况统一走 wittyhub 卸载
        return self._uninstall_wittyhub_skill(normalized_name, agent_id=agent_id)

    def _uninstall_local_skill(
        self,
        skill_name: str,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """删除本地安装的技能目录。"""
        skills_dir = self._get_skills_dir(agent_id)
        dst = skills_dir / skill_name

        try:
            dst = self._validate_path_under_skills_dir(dst, agent_id=agent_id)
        except ValueError as exc:
            raise OpenCodeSkillsUninstallError(
                runtime_type=self.runtime_type,
                skill_name=skill_name,
                reason=str(exc),
            ) from exc

        if not dst.exists():
            logger.warning(
                "uninstall_local_skill: skill dir not found, path=%s", dst
            )
            return {
                "runtime_type": self.runtime_type,
                "skill_name": skill_name,
                "uninstalled": True,
                "uninstall_channel": "local_remove",
            }

        self._remove_path(dst)

        logger.info(
            "uninstall_local_skill success, runtime_type=%s agent_id=%s "
            "skill_name=%s dst=%s",
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

    def _uninstall_wittyhub_skill(
        self,
        skill_name: str,
        agent_id: str | None = None,
    ) -> dict[str, Any]:
        """通过 wittyhub 卸载技能，并从 .agents/skills/ 清理本地文件。"""
        xdg_config_home = self._get_xdg_config_home(agent_id)

        # wittyhub remove 失败不阻断，继续清理本地文件
        result = self._run_wittyhub_command(
            [
                "npx",
                "wittyhub",
                "remove",
                skill_name,
                "--agent",
                "opencode",
                "-y",
            ],
            cwd=xdg_config_home,
            skill_name=skill_name,
            error_cls=OpenCodeSkillsUninstallError,
            raise_on_error=False,
        )

        if result is not None:
            logger.info(
                "uninstall_wittyhub_skill success, runtime_type=%s agent_id=%s "
                "skill_name=%s cwd=%s stdout=%s",
                self.runtime_type,
                agent_id,
                skill_name,
                xdg_config_home,
                result.stdout.strip(),
            )

        # 清理 .agents/skills/ 下的技能目录
        dst = self._get_skills_dir(agent_id) / skill_name
        try:
            dst = self._validate_path_under_skills_dir(dst, agent_id=agent_id)
        except ValueError as exc:
            logger.warning(
                "uninstall_wittyhub_skill: path validation failed, "
                "skipping local cleanup. path=%s reason=%s",
                dst,
                exc,
            )
        else:
            if dst.exists():
                logger.info(
                    "uninstall_wittyhub_skill: removing skill dir, path=%s", dst
                )
                self._remove_path(dst)

        # 清理 skills-lock.json
        skills_lock = xdg_config_home / "skills-lock.json"
        if skills_lock.exists():
            skills_lock.unlink(missing_ok=True)

        return {
            "runtime_type": self.runtime_type,
            "skill_name": skill_name,
            "uninstalled": True,
            "uninstall_channel": "wittyhub",
        }

__all__ = ["OpenCodeSkillService"]
