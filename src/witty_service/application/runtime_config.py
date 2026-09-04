from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# 模型注册表的 provider 是通用厂商名（deepseek/zhipuai/...），而 dsh
# harness initialize 的 provider 是其适配器 id。二者命名空间不同，
# 这里做别名映射，免 start 时被 harness 以 "no adapter registered" 拒绝。
_DSH_PROVIDER_ALIASES: dict[str, str] = {"deepseek": "deepseek-official"}

# 经别名解析后允许的 provider 集合。不在白名单内的 provider 在
# build_start_payload 阶段显式拒绝，避免静默透传后于 start 时必现失败。
_DSH_ALLOWED_PROVIDERS: frozenset[str] = frozenset({"deepseek-official"})


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
        profile: str,
        gateway_port: int,
    ) -> dict[str, Any]:
        """构建 /agent/start 接口的请求体."""
        ...

    def port_metadata_key(self) -> str:
        """返回 sandbox metadata 中存储端口号所用的 key."""
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
        profile: str,
        gateway_port: int,
    ) -> dict[str, Any]:
        return {
            "model_id": model_id,
            "model": model_info,
            "opencode": {
                "serve_port": gateway_port,
                "username": "opencode",
                "password": "",  # nosec B105 - 默认空密码占位，由部署配置注入
                "timeout": 30.0,
                "profile": profile,
            },
        }

    def port_metadata_key(self) -> str:
        return "serve_port"


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
        profile: str,
        gateway_port: int,
    ) -> dict[str, Any]:
        return {
            "model_id": model_id,
            "model": model_info,
            "openclaw": {
                "profile": profile,
                "gateway_port": gateway_port,
            },
        }

    def port_metadata_key(self) -> str:
        return "gateway_port"


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
        profile: str,
        gateway_port: int,
    ) -> dict[str, Any]:
        del profile, gateway_port  # dsh 无 HTTP gateway/控制端口
        provider = model_info.get("provider")
        resolved_provider = _DSH_PROVIDER_ALIASES.get(provider, provider)
        # 仅放过白名单内的 provider , 暂时仅支持 deepseek
        if (
            resolved_provider is not None
            and resolved_provider not in _DSH_ALLOWED_PROVIDERS
        ):
            raise ValueError(
                f"unsupported dsh provider {provider!r}: bundled dsh runtime "
                f"only supports {sorted(_DSH_ALLOWED_PROVIDERS)}"
            )
        return {
            "model_id": model_id,
            "model": model_info,
            "dsh": {
                "provider": resolved_provider,
                "model": model_info.get("name") or model_id,
                "api_key": model_info.get("api_key"),
                "base_url": model_info.get("api_base_url"),
                "max_tokens": model_info.get("max_tokens"),
            },
        }

    def port_metadata_key(self) -> str:
        return "dsh_port"
