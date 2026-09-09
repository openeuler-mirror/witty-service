"""
Agent 模板服务 — 从远程 git 仓库拉取 agent 模板，解析 agent.yaml，
创建 agent 并安装 prompt、skills 等配置。
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import uuid
from importlib.resources import files as resources_files
from pathlib import Path
from typing import Any

import git
import yaml

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

# 预置 skill 仓库按 (git_url@branch) 的模块级锁，避免同名仓库并发 clone/pull
_MAX_URL_LOCKS = 1024
_URL_LOCKS: dict[str, threading.Lock] = {}
_URL_LOCKS_GUARD = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _URL_LOCKS_GUARD:
        lock = _URL_LOCKS.get(key)
        if lock is None:
            if len(_URL_LOCKS) >= _MAX_URL_LOCKS:
                _URL_LOCKS.clear()
            lock = threading.Lock()
            _URL_LOCKS[key] = lock
        return lock


# 预置模板实例化的错误码（S5：集中到服务层，API 复用同一常量与校验逻辑）。
TEMPLATE_NOT_FOUND = "TEMPLATE_NOT_FOUND"
AGENT_ALREADY_INSTANTIATED = "AGENT_ALREADY_INSTANTIATED"
MODEL_REQUIRED = "MODEL_REQUIRED"
MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
MODEL_DISABLED = "MODEL_DISABLED"


def validate_model(
    repository: AgentRepository,
    model_id: str | None,
    *,
    required: bool = False,
) -> Any:
    """校验模型存在性/启用状态，返回 ``ModelRecord``。

    - ``required=True``（预置模板实例化）：model_id 必须给出，否则 ``MODEL_REQUIRED``。
    - ``required=False``（通用创建 agent）：model_id 可为空；一旦给出则必须存在且启用。

    集中到服务层供各 API 端点复用，避免创建 agent 的多条路径校验行为不一致。
    """
    if not model_id:
        if required:
            raise DomainError(
                code=MODEL_REQUIRED,
                message="model_id is required.",
                status_code=400,
            )
        return None
    model = repository.get_model(model_id)
    if model is None:
        raise DomainError(
            code=MODEL_NOT_FOUND,
            message="Model was not found.",
            status_code=400,
            details={"model_id": model_id},
        )
    if not model.enabled:
        raise DomainError(
            code=MODEL_DISABLED,
            message="Model is disabled.",
            status_code=400,
            details={"model_id": model_id},
        )
    return model


def _default_idle_timeout() -> int:
    return int(get_settings().openclaw_gateway.idle_timeout)


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
    # 预置模板扫描（包内元数据，v2 / B3+B4）
    # ------------------------------------------------------------------

    @staticmethod
    def preset_agents_root() -> Any:
        """返回包内 `preset_agents` 目录的 Traversable。

        对源码树（`src/witty_service`）与已安装 wheel 均有效；
        wheel 场景下 package-data 负责把 `preset_agents/**/*` 一并打包。
        """
        return resources_files("witty_service") / "data" / "preset_agents"

    @classmethod
    def scan_preset_templates(cls) -> list[AgentTemplate]:
        """扫描包内 `preset_agents` 下全部模板并解析 agent.yaml（零网络、零 DB）。

        未找到目录时返回空列表并记 warning；单个目录缺 agent.yaml 时跳过并记 warning。
        """
        root = cls.preset_agents_root()
        if not root.is_dir():
            logger.warning("preset_agents 目录不存在或被排除：%s", root)
            return []
        templates: list[AgentTemplate] = []
        for entry in sorted(root.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            yaml_path = entry.joinpath("agent.yaml")
            if not yaml_path.is_file():
                logger.warning("模板目录缺少 agent.yaml：%s", entry)
                continue
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError(f"Invalid agent.yaml in {entry}: expected a dict")
            templates.append(AgentTemplate.model_validate(data))
        return templates

    # ------------------------------------------------------------------
    # skill 仓库缓存（B2）
    # ------------------------------------------------------------------

    def resolve_skill_cache_dir(
        self, git_url: str, branch: str | None = None
    ) -> Path:
        """只计算 skill 内容仓库的本地缓存路径，不触发任何拉取（GET 用）。

        缓存目录与既有 agenthub 模板缓存同根（``TEMPLATE_STORE_DIR``）、
        按 ``_repo_name_from_url`` 独立子目录。
        """
        repo_name = self._repo_name_from_url(git_url)
        return TEMPLATE_STORE_DIR / repo_name

    @staticmethod
    def read_repo_commit(cache_dir: Path) -> str | None:
        """读取缓存仓库当前 HEAD commit；``.git`` 不存在（离线/未克隆）时返回 ``None``。"""
        if not (cache_dir / ".git").exists():
            return None
        try:
            return git.Repo(cache_dir).head.commit.hexsha
        except Exception:
            return None

    @staticmethod
    def _is_skill_repo_cloned(cache_dir: Path) -> bool:
        """用 ``.git`` 哨兵判定仓库是否完整克隆，避免半成品 clone 误判为已缓存。"""
        return cache_dir.exists() and (cache_dir / ".git").exists()

    def ensure_skill_repo(
        self, git_url: str, branch: str | None = None
    ) -> tuple[Path, str]:
        """确保 skill 内容仓库已缓存，返回 ``(缓存根目录, commit hash)``。

        缓存缺失（预热未完成/失败）时同步浅克隆兜底；已克隆则**不拉取**（懒兜底只补缺失，不刷新）。
        同名仓库并发通过模块级 ``(git_url@branch)`` 锁串行化，确保 only 一次克隆。
        """
        branch = branch or "main"
        cache_dir = self.resolve_skill_cache_dir(git_url, branch)
        with _lock_for(f"{git_url}@{branch}"):
            if not self._is_skill_repo_cloned(cache_dir):
                cache_dir.parent.mkdir(parents=True, exist_ok=True)
                if cache_dir.exists():
                    # 半成品残留（目录存在但无 .git 哨兵）：清空后重新 clone，避免 clone 因目标非空失败
                    shutil.rmtree(cache_dir)
                logger.info("Cloning skill repo: %s -> %s", git_url, cache_dir)
                try:
                    git.Repo.clone_from(git_url, cache_dir, branch=branch, depth=1)
                except git.GitCommandError as exc:
                    logger.error(
                        "Failed to clone skill repo: git_url=%s branch=%s error=%s",
                        git_url,
                        branch,
                        exc,
                    )
                    raise DomainError(
                        code="TEMPLATE_SKILL_REPO_UNREACHABLE",
                        message=f"Failed to clone skill repository: {exc}",
                        details={"git_url": git_url, "branch": branch},
                    ) from exc
                logger.info("Skill repo cloned: %s", cache_dir)

        commit = self.read_repo_commit(cache_dir)
        if commit is None:
            raise DomainError(
                code="TEMPLATE_SKILL_REPO_UNREACHABLE",
                message="Skill repository cache is incomplete.",
                details={"git_url": git_url, "branch": branch, "cache_dir": str(cache_dir)},
            )
        return cache_dir, commit

    def prewarm_skill_repos(self) -> None:
        """遍历包内全部模板声明的 ``skill_source`` 去重，逐个缓存预热。

        缓存缺失则 clone；已存在则 fetch + reset 跟随分支演进。
        每项 ``try/except → logger.warning``，绝不抛出（不阻塞/不中断启动）。
        """
        templates = self.scan_preset_templates()
        seen: set[tuple[str, str | None]] = set()
        for template in templates:
            if template.skill_source is None:
                continue
            key = (template.skill_source.git_url, template.skill_source.branch)
            if key in seen:
                continue
            seen.add(key)
            self._prewarm_skill_repo(
                template.skill_source.git_url, template.skill_source.branch
            )

    def _prewarm_skill_repo(self, git_url: str, branch: str | None) -> None:
        branch = branch or "main"
        cache_dir = self.resolve_skill_cache_dir(git_url, branch)
        try:
            with _lock_for(f"{git_url}@{branch}"):
                if not self._is_skill_repo_cloned(cache_dir):
                    cache_dir.parent.mkdir(parents=True, exist_ok=True)
                    if cache_dir.exists():
                        shutil.rmtree(cache_dir)
                    logger.info("Prewarm cloning skill repo: %s -> %s", git_url, cache_dir)
                    git.Repo.clone_from(git_url, cache_dir, branch=branch, depth=1)
                else:
                    self._pull_skill_repo(cache_dir, branch)
        except Exception as exc:
            logger.warning(
                "Failed to prewarm skill repo: git_url=%s branch=%s error=%s",
                git_url,
                branch,
                exc,
            )

    def _pull_skill_repo(self, cache_dir: Path, branch: str) -> None:
        """已有缓存 → 跟随分支演进（stash 脏工作区后 fetch + reset 到分支）。"""
        repo = git.Repo(cache_dir)
        if repo.is_dirty(untracked_files=True):
            repo.git.stash("--include-untracked")
        origin = repo.remotes.origin
        origin.fetch()
        repo.git.reset("--hard", f"origin/{branch}")
        logger.info("Prewarm updated skill repo to %s (branch=%s)", cache_dir, branch)

    # ------------------------------------------------------------------
    # 预置模板实例化（B4）
    # ------------------------------------------------------------------

    async def instantiate_preset_template(
        self,
        *,
        name: str,
        model_id: str | None,
        sandbox_type: str = "local_process",
    ) -> AgentCreateResult:
        """一键实例化包内预置模板（一模板一实例）。

        编排：校验模板存在 → 校验 model（必填+存在+启用）→ 同名 agent 已存在返回 409 →
        ``ensure_skill_repo`` 懒兜底 → ``create_agent``（adapter 恒为 opencode）→
        安装 skills / AGENTS.md / opencode.json；post-config 任一步失败则整体回滚
        （删除 agent 释放同名）。
        """
        # 1. 校验模板存在（扫包内）
        template = self._find_preset_template(name)
        if template is None:
            raise DomainError(
                code=TEMPLATE_NOT_FOUND,
                message="Agent template was not found.",
                status_code=404,
                details={"name": name},
            )

        # 2. model 校验（缺失 / 不存在 / 禁用）
        model = validate_model(self._repository, model_id, required=True)

        # 3. 同名 agent 已存在 → 409（一模板一实例）
        existing = self._find_agent_by_name(template.name)
        if existing is not None:
            raise DomainError(
                code=AGENT_ALREADY_INSTANTIATED,
                message="An agent with the same name already exists.",
                status_code=409,
                details={"name": template.name, "agent_id": existing.id},
            )

        # 4. adapter 恒为 opencode；sandbox_type 缺省 local_process
        adapter_type = "opencode"
        sandbox_type = sandbox_type or "local_process"

        # 5. ensure_skill_repo（B2 懒兜底）
        cache_dir: Path | None = None
        if template.skill_source is not None:
            cache_dir, _ = self.ensure_skill_repo(
                template.skill_source.git_url,
                template.skill_source.branch,
            )

        # 6. 创建 agent（复用既有事务性 create_agent 流程）
        agent_manager = self._agent_manager_factory(sandbox_type)
        create_request = AgentCreateRequest(
            name=template.name,
            description=template.description,
            sandbox_type=sandbox_type,
            adapter_type=adapter_type,
            idle_timeout_seconds=_default_idle_timeout(),
            model_id=model.id,
            mcp_server_list=[],
        )
        result = agent_manager.create_agent(create_request)
        logger.info(
            "Agent created from preset template: agent_id=%s name=%s",
            result.agent.id,
            template.name,
        )

        # 7. post-create 配置块（任一步抛错 → 整体回滚释放同名）
        try:
            self._apply_preset_post_config(
                agent_manager=agent_manager,
                agent=result.agent,
                template=template,
                cache_dir=cache_dir,
            )
        except Exception:
            logger.exception(
                "Failed to apply template post-create config; rolling back agent: %s",
                result.agent.id,
            )
            try:
                await agent_manager.delete_agent(result.agent.id)
            except Exception:
                logger.error(
                    "Failed to roll back agent after post-create failure: agent_id=%s",
                    result.agent.id,
                    exc_info=True,
                )
            raise

        return result

    def _find_preset_template(self, name: str) -> AgentTemplate | None:
        for template in self.scan_preset_templates():
            if template.name == name:
                return template
        return None

    def _find_agent_by_name(self, name: str) -> AgentRecord | None:
        for agent in self._repository.list_agents():
            if agent.name == name:
                return agent
        return None

    def _apply_preset_post_config(
        self,
        *,
        agent_manager: AgentManager,
        agent: AgentRecord,
        template: AgentTemplate,
        cache_dir: Path | None,
    ) -> None:
        """``create_agent`` 成功后的配置安装块（预置模板）。

        a. 拷贝顶层 skills → ``<workspace>/.agents/skills/<name>/``
        b. ``sync_installed_agent_skills`` → runtime 自枚举顶层为 builtin（嵌套为载荷不落库）
        c. 写 ``<workspace>/AGENTS.md`` = ``template.prompt.system``（仅在非空时写）
        d. 合并写 ``<workspace>/opencode/opencode.json`` 的 ``instructions``（保留已有）

        任一步抛错即向上传播，由调用方整体回滚。
        """
        workspace = Path(agent.workspace_path)

        # a. 拷贝顶层 skills（嵌套子技能随目录拷贝，不单独登记）
        if cache_dir is not None and template.skills:
            self._install_preset_skills(workspace, template, cache_dir)

        # b. runtime 自枚举顶层为 builtin（幂等）
        agent_manager.sync_installed_agent_skills(agent.id)

        # c. 写 AGENTS.md（在 create_agent/onboard 之后，避免被种子化覆盖）
        if template.prompt.system:
            (workspace / "AGENTS.md").write_text(template.prompt.system, encoding="utf-8")
            logger.info("Wrote AGENTS.md: agent_id=%s", agent.id)

        # d. 合并 opencode.json 的 instructions（保留已有 model/provider/mcp）
        self._merge_opencode_instructions(workspace)

    def _install_preset_skills(
        self, workspace: Path, template: AgentTemplate, cache_dir: Path
    ) -> None:
        """把模板声明的顶层 skill 目录拷贝到 opencode workspace 的 ``.agents/skills/``。

        source 为相对仓库（``cache_dir``）的路径；若声明路径不存在，回退按 skill 名
        （``**/<name>/SKILL.md``）在仓库内搜索，以容忍上游仓库目录结构调整。
        """
        skills_dir = workspace / ".agents" / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        for skill in template.skills:
            if not skill.source:
                continue
            source_path = template.resolve_skill_source_path(skill, Path(cache_dir))
            if source_path is None or not source_path.exists():
                source_path = self._find_skill_dir_by_name(Path(cache_dir), skill.name)
            if source_path is None:
                logger.warning(
                    "Template skill source not found, skipping: name=%s source=%s",
                    skill.name,
                    skill.source,
                )
                continue
            dest_path = skills_dir / skill.name
            if dest_path.exists():
                shutil.rmtree(dest_path)
            shutil.copytree(source_path, dest_path)
            logger.info(
                "Copied template skill to opencode workspace: %s -> %s",
                source_path,
                dest_path,
            )

    def _find_skill_dir_by_name(self, cache_dir: Path, skill_name: str) -> Path | None:
        """在仓库 ``skills/`` 下按目录名搜索 skill（容忍路径层级差异）。"""
        skills_root = cache_dir / "skills"
        if not skills_root.is_dir():
            return None
        matches = sorted(
            p.parent for p in skills_root.glob(f"**/{skill_name}/SKILL.md")
        )
        return matches[0] if matches else None

    def _merge_opencode_instructions(self, workspace: Path) -> None:
        """合并写 ``opencode/opencode.json`` 的 ``instructions``，保留已有 model/provider/mcp。

        M5：不覆盖已有 ``instructions``——追加去重；且仅当 ``AGENTS.md`` 确实写出时才引用，
        避免悬空指令。
        """
        opencode_dir = workspace / "opencode"
        opencode_dir.mkdir(parents=True, exist_ok=True)
        config_path = opencode_dir / "opencode.json"
        config: dict[str, Any] = {}
        if config_path.exists():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    config = loaded
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to read existing opencode.json: %s", exc)

        existing = config.get("instructions", [])
        if not isinstance(existing, list):
            existing = []
        merged = list(existing)
        if (workspace / "AGENTS.md").exists() and "AGENTS.md" not in merged:
            merged.append("AGENTS.md")
        config["instructions"] = merged
        config_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Merged opencode.json instructions: %s", config_path)

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
