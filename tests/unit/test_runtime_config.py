"""OpencodeConfig 的启动环境：单 runtime 不再向子进程传递 Runtime 选择变量。"""

from __future__ import annotations

from witty_service.application.runtime_config import OpencodeConfig


def test_build_env_does_not_select_runtime() -> None:
    """子进程继承父进程环境后，旧变量仍不得改变 agent-server 的装配。"""
    assert OpencodeConfig().build_env() == {}


def test_build_start_payload_carries_opencode_settings() -> None:
    payload = OpencodeConfig().build_start_payload(
        model_id="model-1",
        model_info={"provider": "openai"},
        agent_key="agent-key",
        gateway_port=4321,
    )

    assert payload["model_id"] == "model-1"
    assert payload["opencode"]["serve_port"] == 4321
    assert payload["opencode"]["profile"] == "agent-key"
