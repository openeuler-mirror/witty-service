"""
Universal Agent Specification (UAS) v1.0 — 模型定义

用于解析 agent.yaml 模板文件，提供类型安全的访问方式。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


class AgentTemplatePrompt(BaseModel):
    """PROMPT 配置。

    ``system`` 是给 agent 的角色/流程指令；``default`` 是给**用户**的默认使用提问，
    随 ``GET /agent-templates`` 暴露（``default_prompt``），由前端渲染成用户可直接发送的
    默认提问——后端不往 ``AGENTS.md`` 里注入任何引导内容。
    """

    system: str | None = None
    system_file: str | None = None
    workflow_file: str | None = None
    default: str | None = None


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


class AgentTemplateMcpServer(BaseModel):
    """v3 扩展：MCP server 声明（模板声明式 MCP）。

    字段与 POST /mcp-servers 的存储态配置对齐（command/args/env/cwd/url/headers），
    经 to_storage_config() 转换后交给 opencode 的 MCP 配置转换器，
    保证与 POST /agent/mcp/enable 走同一条写入路径。

    type 省略时按字段推断：给了 url 即 remote，否则 local。
    """

    name: str
    type: str | None = None
    command: str | list[str] | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    when: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_transport(self) -> AgentTemplateMcpServer:
        if not self.name.strip():
            raise ValueError("mcp server name must not be empty")
        if self.type is not None and self.type not in {"local", "remote"}:
            raise ValueError(
                f"mcp server {self.name!r}: type must be 'local' or 'remote', "
                f"got {self.type!r}"
            )
        if self.resolved_type() == "local":
            if not self.command:
                raise ValueError(f"local mcp server {self.name!r} requires command")
        elif not self.url:
            raise ValueError(f"remote mcp server {self.name!r} requires url")
        return self

    def resolved_type(self) -> str:
        """返回实际传输类型（type 未声明时按 url 推断）。"""
        if self.type:
            return self.type
        return "remote" if self.url else "local"

    def to_storage_config(self) -> dict[str, Any]:
        """转换为 POST /mcp-servers 同构的存储态配置。"""
        config: dict[str, Any] = {}
        if self.resolved_type() == "remote":
            config["url"] = self.url
            if self.headers:
                config["headers"] = dict(self.headers)
        else:
            config["command"] = self.command
            if self.args:
                config["args"] = list(self.args)
            if self.env:
                config["env"] = dict(self.env)
            if self.cwd:
                config["cwd"] = self.cwd
        config["enabled"] = self.enabled
        return config


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
    mcp: list[AgentTemplateMcpServer] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_unique_mcp_names(self) -> AgentTemplate:
        """模板内 MCP server 名唯一——重名会在 opencode.json 里互相覆盖。"""
        names = [server.name for server in self.mcp]
        duplicates = sorted({name for name in names if names.count(name) > 1})
        if duplicates:
            raise ValueError(f"duplicate mcp server name(s): {', '.join(duplicates)}")
        return self

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
