from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from witty_service.application.agent_manager import AgentCreateResult
from witty_service.application.agent_template_service import AgentTemplateService
from witty_service.domain.agent_template import AgentTemplate, AgentTemplateSkill
from witty_service.persistence.repositories import AgentRecord


def _agent_record() -> AgentRecord:
    now = datetime.now(UTC)
    return AgentRecord(
        id="agent-1",
        name="Template Agent",
        description="from template",
        sandbox_type="local_process",
        adapter_type="http",
        status="running",
        sandbox_id=None,
        workspace_path="/tmp/agent-1",
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
    agent = _agent_record()
    template = AgentTemplate(
        name="Template Agent",
        skills=[AgentTemplateSkill(name="helper", inline="# Helper")],
    )
    home = tmp_path / "home"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
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
        home
        / ".openclaw"
        / "workspace-agent-1"
        / "skills"
        / ".inline_skills"
        / "helper.md"
    ).exists()
