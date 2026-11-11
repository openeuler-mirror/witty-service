from __future__ import annotations

import dataclasses

from witty_service.runtime.projection import (
    ProjectionAgent,
    ProjectionInputs,
    ProjectionModel,
    ProjectionSkill,
    agent_internal_name,
    build_projection,
)

_GOLDEN_HASH = "f4df1b30e55a4381aa992d56ab2aee1b8c97ed89f7b9e9031d4c983fbe380e4d"


def _inputs() -> ProjectionInputs:
    return ProjectionInputs(
        agents=(
            ProjectionAgent(
                definition_id="agent-2",
                name="Reviewer",
                description="Reviews changes",
                system_prompt="You review.",
                kind="expert",
                source_type="preset",
                source_ref="reviewer@1",
            ),
            ProjectionAgent(
                definition_id="agent-1",
                name="Primary",
                description="",
                system_prompt="You help.",
                kind="primary",
                source_type="local",
            ),
        ),
        models=(
            ProjectionModel(
                model_id="m-2",
                display_name="Model B",
                provider_id="acme",
                provider_model_id="model-b",
                max_tokens=8192,
                temperature=0.2,
                is_default=False,
                secret_revision=2,
                compatibility="anthropic",
                api_base_url="https://api.example.com/v1",
            ),
            ProjectionModel(
                model_id="m-1",
                display_name="Model A",
                provider_id="acme",
                provider_model_id="model-a",
                max_tokens=32768,
                temperature=0.7,
                is_default=True,
                secret_revision=0,
                compatibility="openai",
            ),
        ),
        skills=(
            ProjectionSkill(skill_name="probe-skill", content_sha256="b" * 64),
            ProjectionSkill(skill_name="alpha-skill", content_sha256="a" * 64),
        ),
    )


def test_projection_is_deterministic_and_uses_stable_agent_names() -> None:
    projection = build_projection(_inputs())

    assert projection.document["agents"] == [
        {
            "internal_name": "ag_agent-1",
            "name": "Primary",
            "description": "",
            "prompt": "You help.",
            "kind": "primary",
            "source": {"type": "local", "ref": None},
        },
        {
            "internal_name": "ag_agent-2",
            "name": "Reviewer",
            "description": "Reviews changes",
            "prompt": "You review.",
            "kind": "expert",
            "source": {"type": "preset", "ref": "reviewer@1"},
        },
    ]
    assert [model["id"] for model in projection.document["models"]] == ["m-1", "m-2"]
    assert [skill["name"] for skill in projection.document["skills"]] == [
        "alpha-skill",
        "probe-skill",
    ]
    assert projection.projection_hash == _GOLDEN_HASH
    assert agent_internal_name("agent-1") == "ag_agent-1"


def test_projection_ignores_input_order() -> None:
    inputs = _inputs()
    reordered = ProjectionInputs(
        agents=tuple(reversed(inputs.agents)),
        models=tuple(reversed(inputs.models)),
        skills=tuple(reversed(inputs.skills)),
    )

    assert (
        build_projection(reordered).canonical_json
        == build_projection(inputs).canonical_json
    )


def test_projection_orders_skills_with_casefold_collisions() -> None:
    skills = (
        ProjectionSkill(skill_name="Straße-skill", content_sha256="a" * 64),
        ProjectionSkill(skill_name="strasse-skill", content_sha256="b" * 64),
    )
    inputs = ProjectionInputs(agents=(), models=(), skills=skills)
    reversed_inputs = ProjectionInputs(
        agents=(), models=(), skills=tuple(reversed(skills))
    )

    assert (
        build_projection(inputs).canonical_json
        == build_projection(reversed_inputs).canonical_json
    )


def test_projection_changes_with_agent_prompt() -> None:
    inputs = _inputs()
    edited = dataclasses.replace(
        inputs,
        agents=(
            dataclasses.replace(inputs.agents[0], system_prompt="You help more."),
            inputs.agents[1],
        ),
    )

    assert build_projection(edited).projection_hash != _GOLDEN_HASH


def test_projection_changes_with_secret_revision_and_skill_digest() -> None:
    inputs = _inputs()
    rotated = dataclasses.replace(
        inputs,
        models=(
            dataclasses.replace(inputs.models[0], secret_revision=3),
            inputs.models[1],
        ),
    )
    updated_skill = dataclasses.replace(
        inputs,
        skills=(
            dataclasses.replace(inputs.skills[0], content_sha256="c" * 64),
            inputs.skills[1],
        ),
    )

    assert build_projection(rotated).projection_hash != _GOLDEN_HASH
    assert build_projection(updated_skill).projection_hash != _GOLDEN_HASH
