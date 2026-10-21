from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class UnsupportedModelProviderError(ValueError):
    """模型与 runtime 不兼容（provider 不支持 / 端点缺失或未实值化）。

    调用方应只捕获本类型，避免把 runtime 抛出的其它 ``ValueError`` 误判为模型问题。
    """


# 注册表 provider 的默认端点：仅当创建/更新模型未传 api_base_url（None）时兜底
# （api/models.py）。
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


class RuntimeConfig(Protocol):
    """Runtime 配置策略接口。

    每种 runtime 类型提供统一的:
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

        ``agent_key`` 是外层 agent 的身份标识（witty agent uuid），runtime 将其
        映射为 ``profile``。

        ``gateway_port`` 对 ``uses_gateway_port()`` 为 False 的 runtime 是 ``None``，
        由该 runtime 自行忽略；其余 runtime 由调用方保证非 ``None``。
        """
        ...

    def port_metadata_key(self) -> str:
        """返回 sandbox metadata 中存储端口号所用的 key（仅限有网关端口的 runtime）."""
        ...

    def uses_gateway_port(self) -> bool:
        """该 runtime 是否真的监听一个 HTTP 网关/控制端口.

        返回 False 的 runtime 不参与端口分配，metadata 里也不留端口键，避免留下
        一个没有任何进程监听的端口号。
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
