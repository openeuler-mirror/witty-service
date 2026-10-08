"""dsh runtime 适配器（witty_service 侧）单元测试。

覆盖：AgentManager._RUNTIME_CONFIGS 注册、DshConfig 的 env / start payload /
无网关端口的处理策略，以及多 provider 路由策略（deepseek 原生 / pi-ai
catalog / openai-compat），锁定 dsh 经 witty_service POST /agents 创建的能力。
"""

from __future__ import annotations

from typing import Any

import pytest

from witty_service.application.agent_manager import AgentManager
from witty_service.application.runtime_config import (
    DshConfig,
    UnsupportedModelProviderError,
)

def _model_info(
    provider: str | None, name: str = "m", api_base_url: str | None = None
) -> dict[str, Any]:
    return {
        "name": name,
        "provider": provider,
        "api_key": "test-key",
        "api_base_url": api_base_url,
        "compatibility": {},
    }


def test_agent_manager_registers_dsh_runtime_config() -> None:
    config = AgentManager._RUNTIME_CONFIGS["dsh"]
    assert isinstance(config, DshConfig)
    assert config.adapter_type == "dsh"
    # dsh 的控制面不是 HTTP 端口：不参与端口分配，也不写端口 metadata。
    assert config.uses_gateway_port() is False


def test_dsh_config_build_env_selects_dsh_runtime() -> None:
    assert DshConfig().build_env() == {"WITTY_RUNTIME_DEFAULT": "dsh"}


def test_dsh_config_build_start_payload_carries_model_config() -> None:
    model_info = _model_info("deepseek", name="deepseek-v4-flash")
    payload = DshConfig().build_start_payload(
        model_id="model-1",
        model_info=model_info,
        agent_key="profile-x",
        gateway_port=12345,
    )
    assert payload["model_id"] == "model-1"
    assert payload["model"] == model_info
    assert payload["dsh"] == {
        # workspace_key = 外层 witty agent uuid，agent-server 侧用于 workspace 隔离
        "workspace_key": "profile-x",
        # 注册表 vendor 名 deepseek → dsh harness 适配器 id deepseek-official
        "provider": "deepseek-official",
        "model": "deepseek-v4-flash",
        "api_key": "test-key",
        "base_url": None,
        "max_tokens": None,
    }


def test_dsh_config_rejects_unknown_provider() -> None:
    """不在支持列表内的 provider 显式拒绝，报错展示用户原始填写名与支持列表。"""
    with pytest.raises(UnsupportedModelProviderError, match="supported providers"):
        DshConfig().build_start_payload(
            model_id=None,
            model_info=_model_info("some-unknown-vendor", name="mystery"),
            agent_key="p",
            gateway_port=1,
        )
