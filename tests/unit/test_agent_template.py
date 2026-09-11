from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock

import pytest
import yaml

from witty_service.application.agent_manager import AgentCreateResult
from witty_service.application.agent_template_service import AgentTemplateService
from witty_service.domain.agent_template import (
    AgentTemplate,
    AgentTemplateMcpServer,
    AgentTemplatePrompt,
    AgentTemplateSkill,
)
from witty_service.persistence.repositories import AgentRecord


def _agent_record(workspace_path: str = "/tmp/agent-1") -> AgentRecord:
    now = datetime.now(UTC)
    return AgentRecord(
        id="agent-1",
        name="Template Agent",
        description="from template",
        sandbox_type="local_process",
        adapter_type="http",
        status="running",
        sandbox_id=None,
        workspace_path=workspace_path,
        idle_timeout_seconds=300,
        model_id=None,
        mcp_server_list=[],
        created_at=now,
        updated_at=now,
        last_active_at=None,
    )


def test_agent_template_loads_yaml_and_resolves_skill_source(tmp_path) -> None:
    skill_file = tmp_path / "skills" / "helper" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text("# Helper", encoding="utf-8")
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        """
uas_version: 1.0.0
name: Template Agent
version: 2.0.0
description: From YAML
author: Witty
tags: [dev, helper]
prompt:
  system: Be helpful
skills:
  - name: helper
    source: skills/helper/SKILL.md
    when: [always]
""".strip(),
        encoding="utf-8",
    )

    template = AgentTemplate.from_yaml(yaml_path)

    assert template.name == "Template Agent"
    assert template.version == "2.0.0"
    assert template.tags == ["dev", "helper"]
    assert template.prompt.system == "Be helpful"
    assert (
        template.resolve_skill_source_path(template.skills[0], tmp_path) == skill_file
    )


def test_agent_template_rejects_non_mapping_yaml(tmp_path) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text("- invalid", encoding="utf-8")

    with pytest.raises(ValueError, match="expected a dict"):
        AgentTemplate.from_yaml(yaml_path)


def test_agent_template_service_lists_template_metadata(tmp_path, monkeypatch) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        """
name: Template Agent
version: 1.2.3
description: Metadata
author: Witty
tags: [demo]
skills:
  - name: helper
    inline: hello
""".strip(),
        encoding="utf-8",
    )
    service = AgentTemplateService(MagicMock(), MagicMock())
    monkeypatch.setattr(service, "_ensure_template_repo", lambda *_args: tmp_path)

    templates = service.get_agent_templates("https://example.com/templates.git")

    assert templates == [
        {
            "name": "Template Agent",
            "version": "1.2.3",
            "description": "Metadata",
            "author": "Witty",
            "tags": ["demo"],
            "skill_count": 1,
        }
    ]


def test_agent_template_service_creates_agent_from_template(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "agent.yaml").write_text(
        """
name: Template Agent
description: From template
skills: []
""".strip(),
        encoding="utf-8",
    )
    manager = MagicMock()
    manager.create_agent.return_value = AgentCreateResult(agent=_agent_record())
    factory = MagicMock(return_value=manager)
    service = AgentTemplateService(MagicMock(), factory)
    monkeypatch.setattr(service, "_ensure_template_repo", lambda *_args: tmp_path)

    result = service.create_agent_from_template(
        git_url="https://example.com/templates.git",
        sandbox_type="local_process",
        adapter_type="http",
        idle_timeout_seconds=300,
        mcp_server_list=["mcp-1"],
    )

    request = manager.create_agent.call_args.args[0]
    assert result.agent.id == "agent-1"
    assert request.name == "Template Agent"
    assert request.description == "From template"
    assert request.mcp_server_list == ["mcp-1"]
    factory.assert_called_once_with("local_process")


def test_agent_template_service_writes_inline_skill(tmp_path) -> None:
    service = AgentTemplateService(MagicMock(), MagicMock())
    skill = AgentTemplateSkill(name="helper", inline="# Helper")

    path = service._write_inline_skill(skill, tmp_path)

    assert path == tmp_path / ".inline_skills" / "helper.md"
    assert path.read_text(encoding="utf-8") == "# Helper"


@pytest.mark.parametrize(
    ("git_url", "expected"),
    [
        ("https://github.com/org/templates.git", "templates"),
        ("https://github.com/org/templates/", "templates"),
    ],
)
def test_repo_name_from_url(git_url: str, expected: str) -> None:
    assert AgentTemplateService._repo_name_from_url(git_url) == expected


def test_install_template_skills_records_inline_skill(tmp_path, monkeypatch) -> None:
    repo = MagicMock()
    service = AgentTemplateService(repo, MagicMock())
    agent = _agent_record(workspace_path=str(tmp_path / "ws"))
    template = AgentTemplate(
        name="Template Agent",
        skills=[AgentTemplateSkill(name="helper", inline="# Helper")],
    )
    monkeypatch.setattr(
        "witty_service.application.agent_template_service.uuid.uuid4",
        lambda: "skill-id",
    )

    service._install_template_skills(
        agent_manager=MagicMock(),
        agent=agent,
        template=template,
        template_dir=tmp_path,
    )

    repo.upsert_installed_agent_skill.assert_called_once_with(
        agent_id="agent-1",
        skill_id="skill-id",
        source_type="local",
        repo_id=None,
        skill_name="helper",
        relative_path=".inline_skills/helper.md",
        metadata=None,
        skill_source=None,
        skill_md_url=None,
    )
    assert (
        Path(tmp_path / "ws") / "skills" / ".inline_skills" / "helper.md"
    ).exists()


def _write_opencode_config(workspace: Path, config: dict) -> None:
    path = workspace / "opencode" / "opencode.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config), encoding="utf-8")


def test_agent_template_parses_mcp_block(tmp_path) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        """
name: Template Agent
mcp:
  - name: openeuler-portal
    type: local
    command: [npx, -y, openeuler-portal-mcp]
    env:
      OPENEULER_TOKEN: ${PORTAL_TOKEN}
    enabled: false
  - name: remote-docs
    url: https://example.com/mcp
    headers:
      Authorization: Bearer x
""".strip(),
        encoding="utf-8",
    )

    template = AgentTemplate.from_yaml(yaml_path)

    local_server, remote_server = template.mcp
    assert local_server.resolved_type() == "local"
    assert local_server.to_storage_config() == {
        "command": ["npx", "-y", "openeuler-portal-mcp"],
        "env": {"OPENEULER_TOKEN": "${PORTAL_TOKEN}"},
        "enabled": False,
    }
    assert remote_server.resolved_type() == "remote"
    assert remote_server.to_storage_config() == {
        "url": "https://example.com/mcp",
        "headers": {"Authorization": "Bearer x"},
        "enabled": True,
    }


@pytest.mark.parametrize(
    "mcp_block",
    [
        "- name: broken\n  type: local",
        "- name: broken\n  type: remote",
        "- name: broken\n  type: stdio\n  command: [npx]",
        "- name: \"\"\n  command: [npx]",
    ],
)
def test_agent_template_rejects_invalid_mcp_server(mcp_block: str) -> None:
    with pytest.raises(ValueError):
        AgentTemplate.model_validate({"name": "T", "mcp": yaml.safe_load(mcp_block)})


def test_agent_template_rejects_duplicate_mcp_names() -> None:
    with pytest.raises(ValueError, match="duplicate mcp server name"):
        AgentTemplate.model_validate(
            {
                "name": "T",
                "mcp": [
                    {"name": "portal", "command": ["npx"]},
                    {"name": "portal", "command": ["npx"]},
                ],
            }
        )


def test_merge_opencode_mcp_writes_declarative_servers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PORTAL_TOKEN", "secret-token")
    workspace = tmp_path / "ws"
    service = AgentTemplateService(MagicMock(), MagicMock())
    template = AgentTemplate(
        name="Template Agent",
        mcp=[
            AgentTemplateMcpServer(
                name="openeuler-portal",
                type="local",
                command=["npx", "-y", "openeuler-portal-mcp"],
                env={"OPENEULER_TOKEN": "${PORTAL_TOKEN}"},
            ),
            AgentTemplateMcpServer(name="remote-docs", url="https://example.com/mcp"),
        ],
    )

    names = service._merge_opencode_mcp(workspace, template)

    assert names == ["openeuler-portal", "remote-docs"]
    config = json.loads(
        (workspace / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert config["mcp"]["openeuler-portal"] == {
        "type": "local",
        "command": ["npx", "-y", "openeuler-portal-mcp"],
        "environment": {"OPENEULER_TOKEN": "secret-token"},
        "enabled": True,
    }
    assert config["mcp"]["remote-docs"] == {
        "type": "remote",
        "url": "https://example.com/mcp",
        "enabled": True,
    }


def test_merge_opencode_mcp_preserves_existing_config(tmp_path) -> None:
    workspace = tmp_path / "ws"
    _write_opencode_config(
        workspace,
        {
            "model": "openai/gpt-4",
            "instructions": ["AGENTS.md"],
            "mcp": {"existing": {"type": "local", "command": ["echo"]}},
        },
    )
    service = AgentTemplateService(MagicMock(), MagicMock())
    template = AgentTemplate(
        name="T", mcp=[AgentTemplateMcpServer(name="portal", command="npx")]
    )

    service._merge_opencode_mcp(workspace, template)

    config = json.loads(
        (workspace / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert config["model"] == "openai/gpt-4"
    assert config["instructions"] == ["AGENTS.md"]
    assert config["mcp"]["existing"] == {"type": "local", "command": ["echo"]}
    assert config["mcp"]["portal"] == {
        "type": "local",
        "command": ["npx"],
        "enabled": True,
    }


def test_merge_opencode_mcp_drops_unset_env_reference(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    workspace = tmp_path / "ws"
    service = AgentTemplateService(MagicMock(), MagicMock())
    template = AgentTemplate(
        name="T",
        mcp=[
            AgentTemplateMcpServer(
                name="portal",
                command="npx",
                env={"TOKEN": "${MISSING_TOKEN}"},
            )
        ],
    )

    service._merge_opencode_mcp(workspace, template)

    config = json.loads(
        (workspace / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert "environment" not in config["mcp"]["portal"]


def test_merge_opencode_mcp_noop_without_declarations(tmp_path) -> None:
    workspace = tmp_path / "ws"
    service = AgentTemplateService(MagicMock(), MagicMock())

    assert service._merge_opencode_mcp(workspace, AgentTemplate(name="T")) == []
    assert not (workspace / "opencode" / "opencode.json").exists()


@pytest.mark.asyncio
async def test_apply_preset_post_config_writes_mcp_and_agents_md(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    agent = _agent_record(workspace_path=str(workspace))
    manager = MagicMock()
    service = AgentTemplateService(MagicMock(), MagicMock())
    service._repository.get_sandbox_state.return_value = None
    template = AgentTemplate(
        name="Template Agent",
        prompt=AgentTemplatePrompt(system="# System"),
        mcp=[AgentTemplateMcpServer(name="portal", command=["npx", "portal-mcp"])],
    )

    await service._apply_preset_post_config(
        agent_manager=manager,
        agent=agent,
        template=template,
        cache_dir=None,
    )

    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "# System"
    config = json.loads(
        (workspace / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert config["instructions"] == ["AGENTS.md"]
    assert config["mcp"]["portal"] == {
        "type": "local",
        "command": ["npx", "portal-mcp"],
        "enabled": True,
    }
    manager.sync_installed_agent_skills.assert_called_once_with("agent-1")


class _FakeAdaptorClient:
    """记录 runtime MCP 下发调用的假客户端。"""

    calls: ClassVar[list[tuple[str, object]]] = []
    error: ClassVar[Exception | None] = None

    def __init__(self, *, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url

    async def post(self, path: str, *, json: object = None) -> None:
        self.calls.append((path, json))
        if self.error is not None:
            raise self.error

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_enable_template_mcp_runtime_posts_to_adaptor(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "witty_service.application.agent_template_service.AdaptorHttpClient",
        _FakeAdaptorClient,
    )
    _FakeAdaptorClient.calls = []
    _FakeAdaptorClient.error = None
    repository = MagicMock()
    repository.get_sandbox_state.return_value = SimpleNamespace(
        adapter_base_url="http://127.0.0.1:9000"
    )
    service = AgentTemplateService(repository, MagicMock())
    template = AgentTemplate(
        name="T", mcp=[AgentTemplateMcpServer(name="portal", command=["npx", "portal"])]
    )

    await service._enable_template_mcp_runtime(agent=_agent_record(), template=template)

    assert _FakeAdaptorClient.calls == [
        (
            "/agent/mcp/enable",
            {
                "mcp_server_name": "portal",
                "mcp_server_config": {"command": ["npx", "portal"], "enabled": True},
            },
        )
    ]


@pytest.mark.asyncio
async def test_enable_template_mcp_runtime_swallows_adaptor_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "witty_service.application.agent_template_service.AdaptorHttpClient",
        _FakeAdaptorClient,
    )
    _FakeAdaptorClient.calls = []
    _FakeAdaptorClient.error = RuntimeError("adaptor down")
    repository = MagicMock()
    repository.get_sandbox_state.return_value = SimpleNamespace(
        adapter_base_url="http://127.0.0.1:9000"
    )
    service = AgentTemplateService(repository, MagicMock())
    template = AgentTemplate(
        name="T", mcp=[AgentTemplateMcpServer(name="portal", command=["npx", "portal"])]
    )

    await service._enable_template_mcp_runtime(agent=_agent_record(), template=template)

    assert _FakeAdaptorClient.calls[0][0] == "/agent/mcp/enable"




def test_agent_template_parses_default_prompt(tmp_path) -> None:
    yaml_path = tmp_path / "agent.yaml"
    yaml_path.write_text(
        """
name: Template Agent
prompt:
  system: |
    你是 X。
  default: 帮我做一次巡检。
""".strip(),
        encoding="utf-8",
    )

    template = AgentTemplate.from_yaml(yaml_path)

    assert template.prompt.default == "帮我做一次巡检。"


def test_agent_template_default_prompt_is_optional() -> None:
    template = AgentTemplate.model_validate({"name": "T"})

    assert template.prompt.default is None


@pytest.mark.asyncio
async def test_apply_preset_post_config_does_not_inject_default_prompt(tmp_path) -> None:
    """默认提问只走 API 给前端，后端不得改写 AGENTS.md。"""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    service = AgentTemplateService(MagicMock(), MagicMock())
    service._repository.get_sandbox_state.return_value = None
    template = AgentTemplate(
        name="Template Agent",
        prompt=AgentTemplatePrompt(
            system="# System\n你是 X",
            default="帮我做一次巡检。",
        ),
    )

    await service._apply_preset_post_config(
        agent_manager=MagicMock(),
        agent=_agent_record(workspace_path=str(workspace)),
        template=template,
        cache_dir=None,
    )

    assert (workspace / "AGENTS.md").read_text(encoding="utf-8") == "# System\n你是 X"
def test_preset_templates_declare_mcp_and_skills() -> None:
    templates = {template.name: template for template in AgentTemplateService.scan_preset_templates()}
    expected = {
        "log-diagnosis-agent",
        "security-cve-agent",
        "cluster-container-agent",
        "system-lifecycle-agent",
    }

    assert expected <= set(templates)
    for name in sorted(expected):
        template = templates[name]
        assert len(template.skills) == 6
        assert [server.name for server in template.mcp] == ["openeuler-portal"]
        assert template.skill_source is not None
        assert template.skill_source.git_url.endswith("witty-agents.git")
        assert all(skill.source for skill in template.skills)
        assert template.prompt.system
        assert template.prompt.default
