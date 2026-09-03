"""dsh runtime 适配器（witty_service 侧）单元测试。

覆盖：AgentManager._RUNTIME_CONFIGS 注册、DshConfig 的 env / start payload /
端口 metadata key 策略，锁定 dsh 经 witty_service POST /agents 创建的能力。
"""

from __future__ import annotations

import pytest

from witty_service.application.agent_manager import AgentManager
from witty_service.application.runtime_config import DshConfig


def test_agent_manager_registers_dsh_runtime_config() -> None:
    config = AgentManager._RUNTIME_CONFIGS["dsh"]
    assert isinstance(config, DshConfig)
    assert config.adapter_type == "dsh"
    assert config.port_metadata_key() == "dsh_port"


def test_dsh_config_build_env_selects_dsh_runtime() -> None:
    assert DshConfig().build_env() == {"WITTY_RUNTIME_DEFAULT": "dsh"}


def test_dsh_config_build_start_payload_carries_model_config() -> None:
    model_info = {
        "name": "deepseek-v4-flash",
        "provider": "deepseek",
        "api_key": "test-key",
        "api_base_url": None,
        "compatibility": {},
    }
    payload = DshConfig().build_start_payload(
        model_id="model-1",
        model_info=model_info,
        profile="profile-x",
        gateway_port=12345,
    )
    assert payload["model_id"] == "model-1"
    assert payload["model"] == model_info
    assert payload["dsh"] == {
        # 注册表 vendor 名 deepseek → dsh harness 适配器 id deepseek-official
        "provider": "deepseek-official",
        "model": "deepseek-v4-flash",
        "api_key": "test-key",
        "base_url": None,
        "max_tokens": None,
    }


def test_dsh_config_rejects_unknown_provider() -> None:
    """不在白名单内的 provider 显式拒绝，不做静默透传。"""
    with pytest.raises(ValueError, match="unsupported dsh provider"):
        DshConfig().build_start_payload(
            model_id=None,
            model_info={"name": "glm-5.2", "provider": "zhipuai"},
            profile="p",
            gateway_port=1,
        )
