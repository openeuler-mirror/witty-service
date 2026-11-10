from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

AGENT_NAME_PREFIX = "ag_"


@dataclass(frozen=True, slots=True)
class ProjectionAgent:
    definition_id: str
    name: str
    description: str
    system_prompt: str
    kind: str
    source_type: str
    source_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectionModel:
    model_id: str
    display_name: str
    provider_id: str
    provider_model_id: str
    max_tokens: int
    temperature: float
    is_default: bool
    secret_revision: int
    compatibility: str | None = None
    api_base_url: str | None = None


@dataclass(frozen=True, slots=True)
class ProjectionSkill:
    skill_name: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class ProjectionInputs:
    agents: tuple[ProjectionAgent, ...]
    models: tuple[ProjectionModel, ...]
    skills: tuple[ProjectionSkill, ...]


@dataclass(frozen=True, slots=True)
class RuntimeProjection:
    document: dict[str, Any]
    canonical_json: str
    projection_hash: str


def build_projection(inputs: ProjectionInputs) -> RuntimeProjection:
    """把全量输入规范化为确定性投影。

    调用方只传入未归档 Agent 与 enabled Model/Skill；本函数不做增量合并，
    也不感知 Session 状态或凭据本体，相同输入必然得到相同文档与 hash。
    """
    document = {
        "agents": [
            {
                "internal_name": agent_internal_name(agent.definition_id),
                "name": agent.name,
                "description": agent.description,
                "prompt": agent.system_prompt,
                "kind": agent.kind,
                "source": {"type": agent.source_type, "ref": agent.source_ref},
            }
            for agent in sorted(inputs.agents, key=lambda item: item.definition_id)
        ],
        "models": [
            {
                "id": model.model_id,
                "display_name": model.display_name,
                "provider_id": model.provider_id,
                "provider_model_id": model.provider_model_id,
                "compatibility": model.compatibility,
                "api_base_url": model.api_base_url,
                "max_tokens": model.max_tokens,
                "temperature": model.temperature,
                "is_default": model.is_default,
                "secret_revision": model.secret_revision,
            }
            for model in sorted(inputs.models, key=_model_sort_key)
        ],
        "skills": [
            {"name": skill.skill_name, "content_sha256": skill.content_sha256}
            for skill in sorted(inputs.skills, key=_skill_sort_key)
        ],
    }
    canonical_json = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return RuntimeProjection(
        document=document,
        canonical_json=canonical_json,
        projection_hash=hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
    )


def agent_internal_name(definition_id: str) -> str:
    return f"{AGENT_NAME_PREFIX}{definition_id}"


def _model_sort_key(model: ProjectionModel) -> tuple[str, str, str]:
    return (model.provider_id, model.provider_model_id, model.api_base_url or "")


def _skill_sort_key(skill: ProjectionSkill) -> tuple[str, str]:
    return (skill.skill_name.casefold(), skill.skill_name)
