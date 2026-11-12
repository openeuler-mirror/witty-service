from __future__ import annotations

import json

import pytest

from witty_agent_server.projection_renderer import (
    OPENCODE_CONFIG_PATH,
    OpenCodeProjectionRenderer,
)
from witty_service.runtime.projection import (
    ProjectionAgent,
    ProjectionInputs,
    ProjectionModel,
    RuntimeProjection,
    build_projection,
)


def _projection() -> RuntimeProjection:
    return build_projection(
        ProjectionInputs(
            agents=(
                ProjectionAgent(
                    definition_id="agent-1",
                    name="Primary",
                    description="",
                    system_prompt="You are Witty.",
                    kind="primary",
                    source_type="builtin",
                ),
            ),
            models=(
                ProjectionModel(
                    model_id="model-1",
                    display_name="Model One",
                    provider_id="provider-1",
                    provider_model_id="model-one",
                    max_tokens=4096,
                    temperature=0.1,
                    is_default=True,
                    secret_revision=2,
                    compatibility="openai",
                    api_base_url="https://example.test/v1",
                ),
            ),
            skills=(),
        )
    )


def test_renderer_emits_primary_agents_and_provider_models() -> None:
    files = OpenCodeProjectionRenderer().render(
        _projection(), {"provider-1": "sk-secret"}
    )

    config = json.loads(files[OPENCODE_CONFIG_PATH])
    assert config["agent"] == {
        "ag_agent-1": {
            "description": "Primary",
            "mode": "primary",
            "prompt": "You are Witty.",
        }
    }
    assert config["model"] == "provider-1/model-one"
    provider = config["provider"]["provider-1"]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"] == {
        "baseURL": "https://example.test/v1",
        "apiKey": "sk-secret",
    }
    assert provider["models"]["model-one"]["name"] == "Model One"


def test_renderer_is_deterministic_and_omits_missing_credentials() -> None:
    renderer = OpenCodeProjectionRenderer()
    projection = _projection()

    first = renderer.render(projection, {})
    second = renderer.render(projection, {})

    assert first == second
    provider = json.loads(first[OPENCODE_CONFIG_PATH])["provider"]["provider-1"]
    assert "apiKey" not in provider["options"]
    assert "sk-secret" not in projection.canonical_json


def test_renderer_rejects_inconsistent_provider_endpoints() -> None:
    projection = build_projection(
        ProjectionInputs(
            agents=(),
            models=(
                ProjectionModel(
                    model_id="model-1",
                    display_name="Model One",
                    provider_id="provider-1",
                    provider_model_id="model-one",
                    max_tokens=4096,
                    temperature=0.1,
                    is_default=True,
                    secret_revision=2,
                    compatibility="openai",
                    api_base_url="https://a.example/v1",
                ),
                ProjectionModel(
                    model_id="model-2",
                    display_name="Model Two",
                    provider_id="provider-1",
                    provider_model_id="model-two",
                    max_tokens=4096,
                    temperature=0.1,
                    is_default=False,
                    secret_revision=2,
                    compatibility="openai",
                    api_base_url="https://b.example/v1",
                ),
            ),
            skills=(),
        )
    )

    with pytest.raises(ValueError, match="inconsistent"):
        OpenCodeProjectionRenderer().render(projection, {})
