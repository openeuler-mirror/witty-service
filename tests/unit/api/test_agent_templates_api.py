from __future__ import annotations

from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import Response

from witty_service.api import agent_templates as agent_templates_api
from witty_service.api.agent_templates import list_agent_templates
from witty_service.api.auth import require_bearer_auth
from witty_service.application.agent_template_service import AgentTemplateService
from witty_service.domain.agent_template import AgentTemplate

NEW_TEMPLATES = (
    "log-diagnosis-agent",
    "security-cve-agent",
    "cluster-container-agent",
    "system-lifecycle-agent",
)


def test_list_agent_templates_exposes_mcp_metadata() -> None:
    services = MagicMock()

    responses = {
        item.name: item
        for item in list_agent_templates(Response(), services, locale="zh-CN")
    }

    for name in NEW_TEMPLATES:
        assert name in responses
        assert responses[name].skill_count == 6
        assert responses[name].mcp_count == 1
        assert responses[name].mcp_servers == ["openeuler-portal"]
        assert responses[name].default_prompt


def test_list_agent_templates_exposes_default_prompt_for_every_template() -> None:
    services = MagicMock()

    responses = list_agent_templates(Response(), services, locale="zh-CN")

    assert responses
    for item in responses:
        assert item.default_prompt, item.name


def test_list_agent_templates_keeps_legacy_templates_mcp_free() -> None:
    services = MagicMock()

    responses = {
        item.name: item
        for item in list_agent_templates(Response(), services, locale="zh-CN")
    }

    assert responses["oeAware-os-perf-optimizer"].mcp_count == 0
    assert responses["oeAware-os-perf-optimizer"].mcp_servers == []


def test_list_agent_templates_localizes_display_text_for_en_us() -> None:
    services = MagicMock()

    responses = {
        item.name: item
        for item in list_agent_templates(Response(), services, locale="en-US")
    }

    template = responses["security-cve-agent"]
    assert template.description.startswith("Security and vulnerability governance")
    assert template.default_prompt.startswith("Run a security checkup")
    assert template.default_prompts
    assert all(
        not any("\u4e00" <= char <= "\u9fff" for char in prompt)
        for prompt in template.default_prompts
    )


LOCALIZED_TEMPLATE = AgentTemplate(
    name="demo-agent",
    description="中文描述",
    i18n={
        "en-US": {
            "description": "English description",
            "prompt": {"default": "English prompt", "examples": ["English example"]},
        }
    },
)


def test_agent_templates_endpoint_negotiates_locale_and_sets_headers(
    monkeypatch,
) -> None:
    """HTTP 层：Accept-Language 决定文案语言，并声明 Vary / Content-Language。"""
    monkeypatch.setattr(
        AgentTemplateService,
        "scan_preset_templates",
        staticmethod(lambda: [LOCALIZED_TEMPLATE]),
    )
    app = FastAPI()
    app.state.services = MagicMock()
    app.include_router(agent_templates_api.router)
    app.dependency_overrides[require_bearer_auth] = lambda: None

    with TestClient(app) as client:
        english = client.get(
            "/agent-templates",
            headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        fallback = client.get(
            "/agent-templates",
            headers={"Accept-Language": "fr-FR"},
        )

    assert english.status_code == 200
    assert english.headers["Content-Language"] == "en-US"
    assert "Accept-Language" in english.headers["Vary"]
    body = english.json()
    assert body[0]["description"] == "English description"
    assert body[0]["default_prompt"] == "English prompt"

    assert fallback.status_code == 200
    assert fallback.headers["Content-Language"] == "zh-CN"
    assert fallback.json()[0]["description"] == "中文描述"
