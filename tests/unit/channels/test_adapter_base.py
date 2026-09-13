"""W11 适配器骨架的测试：能力探测、生命周期幂等、三态默认归类、凭据掩码。"""

from __future__ import annotations

import pytest

from witty_service.channels import errors as err
from witty_service.channels.adapters.base import (
    BaseChannelAdapter,
    resolve_adapter_class,
)
from witty_service.channels.contracts import (
    ADAPTER_REGISTRY,
    CERTAINTY_UNCERTAIN,
    ChannelCapabilities,
    DeliveryResult,
    InboundMessage,
    Route,
)


class _FakeAdapter(BaseChannelAdapter):
    channel = "unit_fake"
    adapter_version = "9.9.9"
    config_fields = ("bot_id",)
    mask_fields = ("bot_id",)

    def __init__(self, *, probe: ChannelCapabilities | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.probe = probe
        self.connects = 0
        self.disconnects = 0
        self.sent: list[str] = []

    async def _connect(self) -> None:
        self.connects += 1

    async def _disconnect(self) -> None:
        self.disconnects += 1

    async def _probe_capabilities(self) -> ChannelCapabilities:
        if self.probe is None:
            raise RuntimeError("probe failed")
        return self.probe

    async def send_text(self, route: Route, text: str) -> DeliveryResult:
        self.sent.append(text)
        return DeliveryResult.delivered_with("ref-1")


class _NoProbeAdapter(_FakeAdapter):
    async def _probe_capabilities(self) -> ChannelCapabilities:
        raise RuntimeError("probe failed")


def _route() -> Route:
    return Route(
        instance_id="i1", conversation_type="direct", platform_user_id="u1"
    )


# ==============================================================================
# 能力声明
# ==============================================================================


def test_capabilities_are_conservative_before_start() -> None:
    adapter = _FakeAdapter()

    capabilities = adapter.capabilities()

    assert capabilities.can_edit_message is False
    assert capabilities.max_text_length > 0


@pytest.mark.asyncio
async def test_capabilities_are_probed_during_start() -> None:
    adapter = _FakeAdapter(
        probe=ChannelCapabilities(
            can_edit_message=True, max_text_length=1234, max_reply_segments=None
        )
    )

    await adapter.start()

    assert adapter.capabilities().max_text_length == 1234
    assert adapter.capabilities().can_edit_message is True


@pytest.mark.asyncio
async def test_probe_failure_falls_back_to_conservative_values() -> None:
    """探测失败必须退回保守值，而不是让渠道起不来（框架设计 §3.2）。"""
    adapter = _NoProbeAdapter()

    await adapter.start()

    assert adapter.capabilities().can_edit_message is False
    assert adapter.started is True


# ==============================================================================
# 生命周期
# ==============================================================================


@pytest.mark.asyncio
async def test_lifecycle_is_idempotent() -> None:
    adapter = _FakeAdapter()

    await adapter.start()
    await adapter.start()
    assert adapter.connects == 1

    await adapter.stop()
    await adapter.stop()
    assert adapter.disconnects == 1


@pytest.mark.asyncio
async def test_stop_without_start_is_noop() -> None:
    adapter = _FakeAdapter()

    await adapter.stop()

    assert adapter.disconnects == 0


# ==============================================================================
# 三态归类
# ==============================================================================


def test_unknown_exception_is_uncertain() -> None:
    """无法确定的异常一律归为 uncertain：误判为 rejected 会导致重复投递。"""
    result = _FakeAdapter().classify_exception(RuntimeError("boom"))

    assert result.certainty == CERTAINTY_UNCERTAIN
    assert result.error_code == err.CHANNEL_DELIVERY_UNCERTAIN


@pytest.mark.asyncio
async def test_edit_text_default_is_uncertain() -> None:
    """默认实现返回三态而不是抛异常。"""
    result = await _FakeAdapter().edit_text(_route(), "ref", "text")

    assert result.certainty == CERTAINTY_UNCERTAIN


@pytest.mark.asyncio
async def test_inbound_handler_receives_messages() -> None:
    received: list[InboundMessage] = []

    async def handler(message: InboundMessage) -> None:
        received.append(message)

    adapter = _FakeAdapter()
    adapter.on_inbound(handler)
    message = InboundMessage(
        platform_event_id="e1", route=_route(), text="hi", received_at=None
    )
    await adapter.emit_inbound(message)

    assert received == [message]
    assert adapter.has_inbound_handler is True


@pytest.mark.asyncio
async def test_emit_without_handler_is_dropped() -> None:
    await _FakeAdapter().emit_inbound(
        InboundMessage(platform_event_id="e1", route=_route(), text="hi")
    )


# ==============================================================================
# 凭据拆分与掩码
# ==============================================================================


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("ab", "**"),
        ("abcdefg", "*******"),
        ("abcdefgh", "abcd****efgh"),
        ("bot-123456789", "bot-****6789"),
    ],
)
def test_mask_credential(value: str, expected: str) -> None:
    assert BaseChannelAdapter.mask_credential(value) == expected


def test_split_credentials_separates_config_from_secrets() -> None:
    """未显式声明为非密的字段一律按密文处理（默认加密）。"""
    material = _FakeAdapter.split_credentials(
        {"bot_id": "bot-12345678", "secret": "very-secret", "extra": "x"}
    )

    assert material.config == {"bot_id": "bot-12345678"}
    assert material.secrets == {"secret": "very-secret", "extra": "x"}
    assert material.mask == "bot-****5678"


def test_build_credential_mask_skips_missing_fields() -> None:
    assert _FakeAdapter.build_credential_mask({}) == ""


# ==============================================================================
# 注册表解析
# ==============================================================================


def test_resolve_adapter_class_from_registry() -> None:
    channel = next(iter(ADAPTER_REGISTRY))

    resolved = resolve_adapter_class(channel)

    assert resolved is not None
    assert issubclass(resolved, BaseChannelAdapter)


def test_resolve_adapter_class_rejects_unknown_and_non_skeleton() -> None:
    assert resolve_adapter_class("nope") is None

    class _NotSkeleton:
        channel = "not_skeleton"

    ADAPTER_REGISTRY["not_skeleton"] = _NotSkeleton  # type: ignore[assignment]
    try:
        assert resolve_adapter_class("not_skeleton") is None
    finally:
        ADAPTER_REGISTRY.pop("not_skeleton", None)
