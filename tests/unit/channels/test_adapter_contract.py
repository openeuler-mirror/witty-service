"""W11/W12 适配器契约测试：**参数化遍历 `ADAPTER_REGISTRY`**。

契约测试遍历注册表而不是手写四个用例，因此新增渠道会自动纳入契约测试——这正是
"怎么保证不漏"的机制性答案（框架设计 §10.1）。当前注册表中只有企业微信（标杆
渠道），其余三个渠道接入时无需改动本文件。
"""

from __future__ import annotations

import pytest

# 导入适配器子包即触发全部渠道自注册（ADAPTER_REGISTRY 的枚举来源）
import witty_service.channels.adapters  # noqa: F401
from witty_service.channels.contracts import ADAPTER_REGISTRY
from witty_service.channels.delivery import (
    PRESENTATION_EDIT_PLACEHOLDER,
    PRESENTATION_SEND_NEW,
    plan,
)

CHANNELS = sorted(ADAPTER_REGISTRY)

#: 契约测试统一使用的最小凭据（各渠道的必填字段由各自适配器声明，此处只求可构造）
SAMPLE_CREDENTIALS = {"bot_id": "contract-bot-1234", "secret": "contract-secret"}


def _adapter(channel: str):
    adapter_cls = ADAPTER_REGISTRY[channel]
    return adapter_cls(instance_id="contract-instance", config={}, credentials=SAMPLE_CREDENTIALS)


def test_registry_is_not_empty() -> None:
    """注册表为空意味着没有任何渠道可用；渠道标识符的取值域就是这里的键。"""
    assert CHANNELS


@pytest.mark.parametrize("channel", CHANNELS)
def test_registry_key_matches_channel_classvar(channel: str) -> None:
    assert ADAPTER_REGISTRY[channel].channel == channel


@pytest.mark.parametrize("channel", CHANNELS)
def test_adapter_version_is_declared(channel: str) -> None:
    """`/version` 需要适配器版本；它是 ClassVar，不属于能力声明。"""
    version = ADAPTER_REGISTRY[channel].adapter_version

    assert isinstance(version, str)
    assert version.strip()


@pytest.mark.parametrize("channel", CHANNELS)
def test_capabilities_are_self_consistent(channel: str) -> None:
    capabilities = _adapter(channel).capabilities()

    assert isinstance(capabilities.can_edit_message, bool)
    assert isinstance(capabilities.max_text_length, int)
    assert capabilities.max_text_length > 0
    if capabilities.max_reply_segments is not None:
        assert capabilities.max_reply_segments >= 1


@pytest.mark.parametrize("channel", CHANNELS)
def test_declared_capabilities_have_fallback_paths(channel: str) -> None:
    """声明的能力都必须有降级路径：分段不越界，条数上限保留提示条。"""
    capabilities = _adapter(channel).capabilities()
    text = ("段落。" * 200 + "\n\n") * 6

    result = plan(text, capabilities)

    assert result.actions
    for action in result.actions:
        assert len(action.text) <= capabilities.max_text_length
    if capabilities.max_reply_segments is not None:
        assert len(result.actions) <= capabilities.max_reply_segments
        if result.truncated:
            assert result.actions[-1].is_console_notice is True


@pytest.mark.parametrize("channel", CHANNELS)
def test_presentation_follows_declared_capability(channel: str) -> None:
    """`can_edit_message=False` 时呈现方式必须是"补发新消息"，`edit_text` 不被使用。"""
    capabilities = _adapter(channel).capabilities()

    result = plan("最终结果", capabilities)

    if capabilities.can_edit_message:
        assert result.presentation == PRESENTATION_EDIT_PLACEHOLDER
    else:
        assert result.presentation == PRESENTATION_SEND_NEW


@pytest.mark.parametrize("channel", CHANNELS)
@pytest.mark.asyncio
async def test_lifecycle_is_idempotent(
    channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生命周期模板方法幂等：**连接健康时**重复 start / stop 只产生一次连接与一次断开。

    这里替换掉渠道自己的 `_connect` / `_disconnect`，契约测试不碰真实网络；
    同时把 `is_alive` 换成与 `started` 等价（等价于"连接一直健康"）。否则适配器的
    `is_alive` 会因为被替换掉的 `_connect` 没建立真实连接而报"已死"，从而命中
    "连接已死 -> 重建"分支——那是下一条用例的场景。
    """
    adapter_cls = ADAPTER_REGISTRY[channel]
    calls = {"connect": 0, "disconnect": 0}

    async def _connect(_self) -> None:
        calls["connect"] += 1

    async def _disconnect(_self) -> None:
        calls["disconnect"] += 1

    monkeypatch.setattr(adapter_cls, "_connect", _connect, raising=False)
    monkeypatch.setattr(adapter_cls, "_disconnect", _disconnect, raising=False)
    monkeypatch.setattr(
        adapter_cls, "is_alive", lambda _self: _self.started, raising=False
    )
    adapter = _adapter(channel)

    await adapter.start()
    await adapter.start()
    await adapter.stop()
    await adapter.stop()

    assert calls == {"connect": 1, "disconnect": 1}


@pytest.mark.parametrize("channel", CHANNELS)
@pytest.mark.asyncio
async def test_start_rebuilds_a_dead_connection(
    channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`start()` 的语义是"确保已连接"：连接已死时必须重建，而不是空转。

    网关的健康复查发现 `is_alive()` 为假后会再次调用 `start()`。如果 `start()` 在
    `started` 为真时直接返回，连接已死的适配器就永远无法被重建——实例在 UI 上永远
    显示"未连接"，网关日志却每轮都记一条"已连接"（2026-09-13 真机事故）。
    """
    adapter_cls = ADAPTER_REGISTRY[channel]
    calls = {"connect": 0, "disconnect": 0}

    async def _connect(_self) -> None:
        calls["connect"] += 1

    async def _disconnect(_self) -> None:
        calls["disconnect"] += 1

    monkeypatch.setattr(adapter_cls, "_connect", _connect, raising=False)
    monkeypatch.setattr(adapter_cls, "_disconnect", _disconnect, raising=False)
    monkeypatch.setattr(adapter_cls, "is_alive", lambda _self: False, raising=False)
    adapter = _adapter(channel)

    await adapter.start()
    await adapter.start()

    assert calls == {"connect": 2, "disconnect": 1}


@pytest.mark.parametrize("channel", CHANNELS)
def test_credential_split_keeps_secrets_out_of_config(channel: str) -> None:
    """非密字段才进 `config`；其余一律按密文处理，掩码只覆盖显式声明的字段。"""
    adapter_cls = ADAPTER_REGISTRY[channel]

    material = adapter_cls.split_credentials(
        {"bot_id": "bot-12345678", "secret": "s3cr3t", "token": "t0ken"}
    )

    assert "secret" not in material.config
    assert "token" not in material.config
    assert material.secrets.get("secret") == "s3cr3t"
    assert "s3cr3t" not in material.mask
