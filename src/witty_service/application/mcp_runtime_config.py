from __future__ import annotations

from copy import deepcopy
from typing import Any

from witty_service.api.services import ServiceContainer
from witty_service.application.cve_service import CveService


class McpRuntimeConfigResolver:
    """将持久化 MCP 配置转换为仅供本次启动使用的运行时配置。"""

    def __init__(self, services: ServiceContainer) -> None:
        self._services = services

    def resolve(
        self,
        mcp_server_name: str,
        mcp_server_config: dict[str, Any],
        *,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        if mcp_server_name == "cvekit_mcp":
            runtime_config = CveService(self._services).build_cvekit_mcp_runtime_config(
                mcp_server_config
            )
            return self._inject_model_runtime_config(runtime_config, model_id)
        return deepcopy(mcp_server_config)

    def _inject_model_runtime_config(
        self, runtime_config: dict[str, Any], model_id: str | None
    ) -> dict[str, Any]:
        if not model_id:
            return runtime_config

        model = self._services.repository.get_model(model_id)
        if model is None:
            return runtime_config

        entry = runtime_config.get("cvekit_mcp", runtime_config)
        env = entry["env"]
        for key, value in (
            ("API_KEY", model.api_key),
            ("LLM_PROVIDER", model.provider),
            ("LLM_BASE_URL", model.api_base_url),
            ("LLM_MODEL_NAME", model.name),
        ):
            if isinstance(value, str) and value.strip():
                env[key] = value.strip()
        return runtime_config

    def sanitize_for_storage(
        self, mcp_server_name: str, mcp_server_config: dict[str, Any]
    ) -> dict[str, Any]:
        if mcp_server_name == "cvekit_mcp":
            return CveService.sanitize_cvekit_mcp_storage_config(mcp_server_config)
        return deepcopy(mcp_server_config)
