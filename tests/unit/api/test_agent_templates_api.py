from __future__ import annotations

from unittest.mock import MagicMock

from witty_service.api.agent_templates import list_agent_templates

NEW_TEMPLATES = (
    "log-diagnosis-agent",
    "security-cve-agent",
    "cluster-container-agent",
    "system-lifecycle-agent",
)


def test_list_agent_templates_exposes_mcp_metadata() -> None:
    services = MagicMock()

    responses = {item.name: item for item in list_agent_templates(services)}

    for name in NEW_TEMPLATES:
        assert name in responses
        assert responses[name].skill_count == 6
        assert responses[name].mcp_count == 1
        assert responses[name].mcp_servers == ["openeuler-portal"]
        assert responses[name].default_prompt


def test_list_agent_templates_exposes_default_prompt_for_every_template() -> None:
    services = MagicMock()

    responses = list_agent_templates(services)

    assert responses
    for item in responses:
        assert item.default_prompt, item.name


def test_list_agent_templates_keeps_legacy_templates_mcp_free() -> None:
    services = MagicMock()

    responses = {item.name: item for item in list_agent_templates(services)}

    assert responses["os-perf-optimizer"].mcp_count == 0
    assert responses["os-perf-optimizer"].mcp_servers == []
