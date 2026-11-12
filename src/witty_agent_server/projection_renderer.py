from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from witty_service.runtime.projection import RuntimeProjection

OPENCODE_CONFIG_PATH = "opencode/opencode.json"

_COMPATIBILITY_TO_NPM: dict[str, str] = {
    "openai": "@ai-sdk/openai-compatible",
    "anthropic": "@ai-sdk/anthropic",
}


class OpenCodeProjectionRenderer:
    """把投影渲染成 OpenCode 配置文件；纯函数，不写盘、不碰进程。"""

    def render(
        self, projection: RuntimeProjection, credentials: Mapping[str, str]
    ) -> Mapping[str, str]:
        document = projection.document
        agents = {
            agent["internal_name"]: {
                "description": agent["description"] or agent["name"],
                "mode": "primary",
                "prompt": agent["prompt"],
            }
            for agent in document["agents"]
        }
        providers: dict[str, Any] = {}
        provider_endpoints: dict[str, tuple[str | None, str | None]] = {}
        for model in document["models"]:
            provider_id = model["provider_id"]
            endpoint = (
                model["api_base_url"] or None,
                model["compatibility"] or "openai",
            )
            previous = provider_endpoints.setdefault(provider_id, endpoint)
            if previous != endpoint:
                raise ValueError(
                    f"Provider {provider_id!r} has inconsistent "
                    "api_base_url/compatibility across models"
                )
            provider = providers.setdefault(provider_id, {})
            if model["api_base_url"]:
                provider["npm"] = _COMPATIBILITY_TO_NPM.get(
                    model["compatibility"] or "openai", "@ai-sdk/openai-compatible"
                )
                provider.setdefault("options", {})["baseURL"] = model["api_base_url"]
            if credential := credentials.get(provider_id):
                provider.setdefault("options", {})["apiKey"] = credential
            provider.setdefault("models", {})[model["provider_model_id"]] = {
                "name": model["display_name"] or model["provider_model_id"],
                "status": "active",
            }

        config: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "agent": agents,
            "provider": providers,
        }
        models = document["models"]
        if models:
            default = next(
                (model for model in models if model["is_default"]), models[0]
            )
            config["model"] = f"{default['provider_id']}/{default['provider_model_id']}"
        return {
            OPENCODE_CONFIG_PATH: json.dumps(
                config, ensure_ascii=False, indent=2, sort_keys=True
            )
        }
