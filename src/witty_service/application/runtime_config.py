from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple, Protocol


class UnsupportedModelProviderError(ValueError):
    """模型与 runtime 不兼容（provider 不支持 / 端点缺失或未实值化）。

    调用方应只捕获本类型，避免把 runtime 抛出的其它 ``ValueError`` 误判为模型问题。
    """


class _DshRoute(NamedTuple):
    """dsh 路由解析结果"""

    provider: str | None
    base_url: str | None
    provider_route: dict[str, Any] | None


# 模型注册表 provider（deepseek/openai/...）与 dsh harness 的路由 id 命名空间
# 不同，归一化与路由集中在此：
# - deepseek → deepseek-official：dsh 原生路径，凭据经 DEEPSEEK_* env，
#   无需 settings 物化。
# - catalog 路由（openai/anthropic/google/kimi→moonshotai-cn）：用 dsh 内置
#   pi-ai 目录的原生协议与端点，仅在用户显式覆盖 api_base_url 时下发 baseURL。
# - openai-compat 路由（glm/minimax/ollama/azure/xai/custom）：协议恒为
#   openai-completions，baseURL 取注册表值。
# 非 deepseek 路由的参数经 ``dsh.provider_route`` 下发，由 agent-server 写入
# per-agent ``<dsh_home>/settings.yaml`` 并注入凭据 env。
_DSH_DEEPSEEK_ROUTE: str = "deepseek-official"

# 注册表 vendor 名 → pi-ai catalog 路由 id。
_DSH_CATALOG_ROUTES: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google": "google",
    "kimi": "moonshotai-cn",
}

# OpenAI-compatible 端点 provider：路由 id 即注册表 provider 名。
_DSH_OPENAI_COMPAT_PROVIDERS: frozenset[str] = frozenset(
    {"glm", "minimax", "ollama", "azure", "xai", "custom"}
)

# 产品侧命名（前端 / openclaw / backport 用 ``zhipuai``/``moonshotai``）与模型
# API 文档、默认端点表命名（``glm``/``kimi``）不一致，在此归一化到 dsh 路由 id。
_DSH_PROVIDER_ALIASES: dict[str, str] = {
    "zhipuai": "glm",
    "moonshotai": "kimi",
    "deepseek-official": "deepseek",  # 旧数据直接填 harness 适配器 id
}

# 注册表 provider 的默认端点：仅当创建/更新模型未传 api_base_url（None）时兜底
# （api/models.py）；dsh 亦据此判断 catalog 路由的 api_base_url 是否为用户显式
# 覆盖（等于默认值即未覆盖）。
DEFAULT_API_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "xai": "https://api.x.ai/v1",
    "ollama": "http://localhost:11434/v1",
    "azure": "https://{resource}.openai.azure.com",
    "deepseek": "https://api.deepseek.com/v1",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "minimax": "https://api.minimax.com/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "custom": "",  # 用户自定义，需通过 api_base_url 指定
}

# pi-ai 路由的凭据 env 名：agent-server 据此把 api_key 注入 harness 子进程 env
# （每个 agent 的 dsh_home 独立，同一时刻仅一条活动路由，固定名即可）。
_DSH_PROVIDER_API_KEY_ENV: str = "WITTY_DSH_PROVIDER_API_KEY"

# 支持 provider 列表（错误信息用），按注册表 vendor 名展示。
_DSH_SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "deepseek",
    *sorted(_DSH_CATALOG_ROUTES),
    *sorted(_DSH_OPENAI_COMPAT_PROVIDERS),
)


class RuntimeConfig(Protocol):
    """Runtime 配置策略接口。

    每种 runtime 类型（opencode / openclaw / ...）提供统一的:
    - 沙箱子进程环境变量
    - /agent/start 请求体
    - 端口在 sandbox metadata 中的存储 key
    - 内存限制

    adapter_type 同时作为 Docker 镜像 tag 使用（镜像命名: <base_image>:<adapter_type>）。
    """

    @property
    def adapter_type(self) -> str: ...

    @property
    def memory_limit(self) -> str:
        """返回 Docker 容器的内存限制（例如 "2048m"）。"""
        ...

    def build_env(self) -> dict[str, str]:
        """构建启动 agent-server 子进程时需要注入的环境变量."""
        ...

    def build_start_payload(
        self,
        *,
        model_id: str | None,
        model_info: dict[str, Any],
        agent_key: str,
        gateway_port: int | None,
    ) -> dict[str, Any]:
        """构建 /agent/start 接口的请求体.

        ``agent_key`` 是外层 agent 的身份标识（witty agent uuid），各 runtime
        自行映射：opencode/openclaw 将其作为 ``profile``，dsh 将其作为
        ``workspace_key``（workspace / dsh_home 隔离的事实来源）。

        ``gateway_port`` 对 ``uses_gateway_port()`` 为 False 的 runtime 是 ``None``，
        由该 runtime 自行忽略；其余 runtime 由调用方保证非 ``None``。
        """
        ...

    def port_metadata_key(self) -> str:
        """返回 sandbox metadata 中存储端口号所用的 key（仅限有网关端口的 runtime）."""
        ...

    def uses_gateway_port(self) -> bool:
        """该 runtime 是否真的监听一个 HTTP 网关/控制端口.

        返回 False 的 runtime（例如 dsh，控制面不在 HTTP 端口上）不参与端口分配，
        metadata 里也不留端口键，避免留下一个没有任何进程监听的端口号。
        """
        ...


@dataclass
class OpencodeConfig(RuntimeConfig):
    adapter_type: str = "opencode"
    memory_limit: str = "2048m"

    def build_env(self) -> dict[str, str]:
        return {"WITTY_RUNTIME_DEFAULT": "opencode"}

    def build_start_payload(
        self,
        *,
        model_id: str | None,
        model_info: dict[str, Any],
        agent_key: str,
        gateway_port: int | None,
    ) -> dict[str, Any]:
        if gateway_port is None:  # 有网关端口的 runtime，调用方保证非 None
            raise ValueError("opencode runtime requires a gateway port")
        return {
            "model_id": model_id,
            "model": model_info,
            "opencode": {
                "serve_port": gateway_port,
                "username": "opencode",
                "password": "",  # nosec B105 - 默认空密码占位，由部署配置注入
                "timeout": 30.0,
                "profile": agent_key,
            },
        }

    def port_metadata_key(self) -> str:
        return "serve_port"

    def uses_gateway_port(self) -> bool:
        return True


@dataclass
class OpenclawConfig(RuntimeConfig):
    adapter_type: str = "openclaw"
    memory_limit: str = "512m"

    def build_env(self) -> dict[str, str]:
        return {"WITTY_RUNTIME_DEFAULT": "openclaw"}

    def build_start_payload(
        self,
        *,
        model_id: str | None,
        model_info: dict[str, Any],
        agent_key: str,
        gateway_port: int | None,
    ) -> dict[str, Any]:
        if gateway_port is None:  # 有网关端口的 runtime，调用方保证非 None
            raise ValueError("openclaw runtime requires a gateway port")
        return {
            "model_id": model_id,
            "model": model_info,
            "openclaw": {
                "profile": agent_key,
                "gateway_port": gateway_port,
            },
        }

    def port_metadata_key(self) -> str:
        return "gateway_port"

    def uses_gateway_port(self) -> bool:
        return True


@dataclass
class DshConfig(RuntimeConfig):
    adapter_type: str = "dsh"
    memory_limit: str = "512m"

    def build_env(self) -> dict[str, str]:
        return {"WITTY_RUNTIME_DEFAULT": "dsh"}

    def build_start_payload(
        self,
        *,
        model_id: str | None,
        model_info: dict[str, Any],
        agent_key: str,
        gateway_port: int | None,
    ) -> dict[str, Any]:
        del gateway_port  # dsh 无 HTTP gateway/控制端口
        route = _resolve_dsh_route(model_id=model_id, model_info=model_info)
        dsh_cfg: dict[str, Any] = {
            "workspace_key": agent_key,
            "provider": route.provider,
            "model": model_info.get("name") or model_id,
            "api_key": model_info.get("api_key"),
            "base_url": route.base_url,
            "max_tokens": model_info.get("max_tokens"),
        }
        if route.provider_route is not None:
            dsh_cfg["provider_route"] = route.provider_route
        return {
            "model_id": model_id,
            "model": model_info,
            "dsh": dsh_cfg,
        }

    def uses_gateway_port(self) -> bool:
        return False


def _resolve_dsh_route(
    *, model_id: str | None, model_info: dict[str, Any]
) -> _DshRoute:
    """模型注册表记录 → dsh 路由（见 ``_DshRoute``）。

    支持的 provider 先归一化（strip/lower + 别名表）再路由；不支持的 provider
    或缺失/占位未实值化的端点抛 ``UnsupportedModelProviderError``，不静默透传
    后于 start 时必现失败。
    """
    raw_provider = model_info.get("provider")
    if raw_provider is None:
        # 未选模型：走 dsh 默认（deepseek-official），维持原语义。
        return _DshRoute(provider=None, base_url=None, provider_route=None)
    normalized = str(raw_provider).strip().lower()
    provider = _DSH_PROVIDER_ALIASES.get(normalized, normalized)
    model_name = model_info.get("name") or model_id or "<unknown>"

    if provider == "deepseek":
        return _DshRoute(
            provider=_DSH_DEEPSEEK_ROUTE,
            base_url=model_info.get("api_base_url"),
            provider_route=None,
        )

    if provider in _DSH_CATALOG_ROUTES:
        api_base_url = model_info.get("api_base_url")
        provider_route: dict[str, Any] = {"api_key_env": _DSH_PROVIDER_API_KEY_ENV}
        # 仅显式覆盖端点时下发 baseURL：注册表默认值与 catalog 原生端点可能
        # 不同构（如 anthropic 的 .../v1 会对 native 协议造成路径重复）。
        if api_base_url and api_base_url != DEFAULT_API_BASE_URLS.get(provider):
            provider_route["base_url"] = api_base_url
        return _DshRoute(
            provider=_DSH_CATALOG_ROUTES[provider],
            base_url=None,
            provider_route=provider_route,
        )

    if provider in _DSH_OPENAI_COMPAT_PROVIDERS:
        base_url = model_info.get("api_base_url") or DEFAULT_API_BASE_URLS.get(
            provider, ""
        )
        if not base_url:
            raise UnsupportedModelProviderError(
                f"dsh runtime requires an explicit api_base_url for model "
                f"{model_name!r} with provider {provider!r} (OpenAI-compatible "
                f"endpoint); set the model's api_base_url and retry"
            )
        if "{" in base_url and "}" in base_url:
            raise UnsupportedModelProviderError(
                f"dsh runtime got an unresolved api_base_url placeholder "
                f"{base_url!r} for model {model_name!r} with provider "
                f"{provider!r}; fill in the actual endpoint URL and retry"
            )
        return _DshRoute(
            provider=provider,
            base_url=None,
            provider_route={
                "api_key_env": _DSH_PROVIDER_API_KEY_ENV,
                "api": "openai-completions",
                "base_url": base_url,
            },
        )

    raise UnsupportedModelProviderError(
        f"dsh runtime does not support model {model_name!r} with provider "
        f"{raw_provider!r}; supported providers: "
        f"{', '.join(_DSH_SUPPORTED_PROVIDERS)}"
    )
