"""
Agent 模板服务 — 从远程 git 仓库拉取 agent 模板，解析 agent.yaml，
创建 agent 并安装 prompt、skills 等配置。
"""

from __future__ import annotations

import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

import git

from witty_service.application.agent_manager import (
    AgentCreateRequest,
    AgentCreateResult,
    AgentManager,
    AgentRepository,
)
from witty_service.config import get_settings
from witty_service.domain.agent_template import AgentTemplate, AgentTemplateSkill
from witty_service.domain.errors import DomainError
from witty_service.persistence.repositories import AgentRecord

logger = logging.getLogger(__name__)

# 模板仓库本地缓存根目录
TEMPLATE_STORE_DIR = get_settings().workspace.root_path() / "agent_templates"


class AgentTemplateService:
    """从 git 模板创建 agent 的服务。"""

    def __init__(
        self,
        repository: AgentRepository,
        agent_manager_factory: Any,  # Callable[[str], AgentManager]
    ) -> None:
        self._repository = repository
        self._agent_manager_factory = agent_manager_factory

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def create_agent_from_template(
        self,
        *,
        git_url: str,
        branch: str = "main",
        sandbox_type: str,
        adapter_type: str,
        idle_timeout_seconds: int,
        sandbox_id: str | None = None,
        model_id: str | None = None,
        mcp_server_list: list[str] | None = None,
    ) -> AgentCreateResult:
        """从远程 git 仓库拉取 agent 模板，解析并创建 agent。"""
        # 1. 克隆 / 拉取模板仓库
        template_dir = self._ensure_template_repo(git_url, branch)

        # 2. 解析 agent.yaml
        template = AgentTemplate.from_yaml(template_dir / "agent.yaml")
        logger.info(
            "Parsed agent template: name=%s version=%s description=%s skills=%d",
            template.name,
            template.version,
            template.description,
            len(template.skills),
        )

        # 3. 创建 agent（name/description 来自模板）
        agent_manager = self._agent_manager_factory(sandbox_type)
        create_request = AgentCreateRequest(
            name=template.name,
            description=template.description,
            sandbox_type=sandbox_type,
            adapter_type=adapter_type,
            idle_timeout_seconds=idle_timeout_seconds,
            sandbox_id=sandbox_id,
            model_id=model_id,
            mcp_server_list=mcp_server_list or [],
        )
        result = agent_manager.create_agent(create_request)
        logger.info(
            "Agent created from template: agent_id=%s name=%s",
            result.agent.id,
            template.name,
        )

        # 4. 安装 skills（agent 已 running，通过 adapter 下发）
        if template.skills:
            self._install_template_skills(
                agent_manager=agent_manager,
                agent=result.agent,
                template=template,
                template_dir=template_dir,
            )

        return result

    def get_agent_templates(
        self,
        git_url: str,
        branch: str = "main",
    ) -> list[dict[str, Any]]:
        """查看远程仓库中可用的模板信息（不创建 agent）。"""
        template_dir = self._ensure_template_repo(git_url, branch)
        template = AgentTemplate.from_yaml(template_dir / "agent.yaml")
        return [
            {
                "name": template.name,
                "version": template.version,
                "description": template.description,
                "author": template.author,
                "tags": template.tags,
                "skill_count": len(template.skills),
            }
        ]

    # ------------------------------------------------------------------
    # 仓库管理
    # ------------------------------------------------------------------

    def _ensure_template_repo(self, git_url: str, branch: str) -> Path:
        """克隆或拉取模板仓库，返回本地路径。"""
        TEMPLATE_STORE_DIR.mkdir(parents=True, exist_ok=True)

        repo_name = self._repo_name_from_url(git_url)
        local_path = TEMPLATE_STORE_DIR / repo_name

        if local_path.exists():
            # 已有缓存 → git pull 更新
            logger.info("Updating existing template repo: %s", local_path)
            try:
                repo = git.Repo(local_path)
                # 确保工作目录干净，避免 pull 冲突
                if repo.is_dirty(untracked_files=True):
                    repo.git.stash("--include-untracked")
                origin = repo.remotes.origin
                origin.fetch()
                origin.pull(branch)
                logger.info("Template repo updated: %s (branch=%s)", local_path, branch)
            except git.GitCommandError as exc:
                logger.warning("Failed to update template repo, using cached: %s", exc)
        else:
            # 首次克隆
            logger.info("Cloning template repo: %s -> %s", git_url, local_path)
            try:
                git.Repo.clone_from(git_url, local_path, branch=branch, depth=1)
                logger.info("Template repo cloned: %s", local_path)
            except git.GitCommandError as exc:
                raise DomainError(
                    code="TEMPLATE_REPO_CLONE_FAILED",
                    message=f"Failed to clone template repository: {exc}",
                    details={"git_url": git_url, "branch": branch},
                ) from exc

        return local_path

    # ------------------------------------------------------------------
    # Skills 安装
    # ------------------------------------------------------------------

    def _install_template_skills(
        self,
        *,
        agent_manager: AgentManager,
        agent: AgentRecord,
        template: AgentTemplate,
        template_dir: Path,
    ) -> None:
        """逐个安装模板中定义的 skills。"""
        # 统一到 agent 的 witty workspace（agent.workspace_path，即 artifact 校验/文件端点同源路径）
        openclaw_skills_dir = Path(agent.workspace_path) / "skills"
        openclaw_skills_dir.mkdir(parents=True, exist_ok=True)

        repo = self._repository

        for skill in template.skills:
            source_path = template.resolve_skill_source_path(skill, template_dir)
            if source_path is None and skill.inline:
                # inline skill — 写入临时文件再安装
                source_path = self._write_inline_skill(skill, template_dir)

            logger.info(
                "Installing skill: name=%s source=%s",
                skill.name,
                source_path or "(inline)",
            )

            try:
                # 1. 拷贝 skill 目录到 ~/.openclaw/skills/
                if source_path and source_path.exists():
                    # 获取技能目录（如果 source_path 是文件，则取其父目录）
                    skill_dir = (
                        source_path.parent if source_path.is_file() else source_path
                    )
                    dest_path = openclaw_skills_dir / skill_dir.name

                    # 如果目标目录已存在，先删除
                    if dest_path.exists():
                        shutil.rmtree(dest_path)

                    # 拷贝整个目录
                    shutil.copytree(skill_dir, dest_path)
                    logger.info(
                        "Skill directory copied to openclaw: %s -> %s",
                        skill_dir,
                        dest_path,
                    )

                # 2. 记录到数据库
                logger.info(
                    "Recording skill to DB: agent_id=%s skill=%s", agent.id, skill.name
                )
                try:
                    skill_id = str(uuid.uuid4())
                    relative_path = (
                        str(source_path.relative_to(template_dir))
                        if source_path
                        else None
                    )
                    repo.upsert_installed_agent_skill(
                        agent_id=agent.id,
                        skill_id=skill_id,
                        source_type="local",
                        repo_id=None,
                        skill_name=skill.name,
                        relative_path=relative_path,
                        metadata=None,
                        skill_source=skill.source,
                        skill_md_url=None,
                    )
                    logger.info(
                        "Skill recorded to DB successfully: agent_id=%s skill=%s skill_id=%s",
                        agent.id,
                        skill.name,
                        skill_id,
                    )
                except Exception as db_exc:
                    logger.error(
                        "Failed to record skill to DB: agent_id=%s skill=%s error=%s",
                        agent.id,
                        skill.name,
                        db_exc,
                    )
                    raise

            except Exception as exc:
                logger.warning(
                    "Failed to copy or record skill, continuing: agent_id=%s skill=%s error=%s",
                    agent.id,
                    skill.name,
                    exc,
                )

    def _write_inline_skill(
        self, skill: AgentTemplateSkill, template_dir: Path
    ) -> Path:
        """将 inline skill 写入临时文件，返回路径。"""
        inline_dir = template_dir / ".inline_skills"
        inline_dir.mkdir(exist_ok=True)
        skill_file = inline_dir / f"{skill.name}.md"
        skill_file.write_text(skill.inline, encoding="utf-8")
        return skill_file

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _repo_name_from_url(git_url: str) -> str:
        """从 git URL 提取仓库名。"""
        name = git_url.rstrip("/").split("/")[-1]
        if name.endswith(".git"):
            name = name[:-4]
        return name
