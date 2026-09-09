"""
Universal Agent Specification (UAS) v1.0 — 模型定义

用于解析 agent.yaml 模板文件，提供类型安全的访问方式。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class AgentTemplatePrompt(BaseModel):
    """PROMPT 配置"""

    system: str | None = None
    system_file: str | None = None
    workflow_file: str | None = None


class AgentTemplateSkill(BaseModel):
    """SKILL 配置项"""

    name: str
    source: str | None = None
    inline: str | None = None
    installed: str | None = None
    when: list[str] = Field(default_factory=list)


class AgentTemplateSkillSource(BaseModel):
    """v2 扩展：skill 内容的来源声明（上游 git 仓库 + 分支）"""

    git_url: str
    branch: str | None = None


class AgentTemplate(BaseModel):
    """完整的 UAS v1.0 agent 模板"""

    uas_version: str = "1.0.0"
    name: str
    version: str = "1.0.0"
    description: str = ""
    author: str | None = None
    license: str | None = None
    tags: list[str] = Field(default_factory=list)
    prompt: AgentTemplatePrompt = Field(default_factory=AgentTemplatePrompt)
    skills: list[AgentTemplateSkill] = Field(default_factory=list)
    skill_source: AgentTemplateSkillSource | None = None

    @classmethod
    def from_yaml(cls, yaml_path: str | Path) -> AgentTemplate:
        """从 YAML 文件加载并解析为 AgentTemplate。"""
        import yaml

        yaml_path = Path(yaml_path)
        with open(yaml_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        if not isinstance(data, dict):
            raise ValueError(f"Invalid agent.yaml: expected a dict, got {type(data).__name__}")

        return cls(**data)

    def resolve_skill_source_path(self, skill: AgentTemplateSkill, template_dir: Path) -> Path | None:
        """解析 skill 的 source 路径（相对于 agent.yaml 所在目录）。"""
        if skill.source:
            return (template_dir / skill.source).resolve()
        return None
