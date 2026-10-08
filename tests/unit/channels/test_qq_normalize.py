"""QQ 适配器的归一化与被动／主动投递用例，断言的是**线格式与平台约束**。

录制帧字段名取自官方 SDK `@tencent-connect/qqbot-nodejs@1.0.4` 与 `qq-botpy==1.2.1`：
网关事件的 `d` 字段、被动回复体必须带 `msg_id + msg_seq`、主动消息体两者都不带、
单条 4500 字符、一条入站消息最多 4 条被动回复。

传输层协议见 `test_qq_transport.py`，扫码接入见 `test_qq_provisioning_driver.py`。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from witty_service.channels import errors as err
from witty_service.channels.adapters import qq_bot
from witty_service.channels.adapters.qq_bot import (
    EVENT_C2C_MESSAGE_CREATE,
    EVENT_GROUP_AT_MESSAGE_CREATE,
    MAX_REPLY_SEGMENTS,
    MAX_TEXT_LENGTH,
    PASSIVE_REPLY_FALLBACK_RULE,
    PASSIVE_REPLY_RULES,
    QqBotAdapter,
    QqTransportError,
    build_native_transport,
    normalize_event,
    outbound_target,
    passive_reply_rule,
)
from witty_service.channels.adapters.qq_transport import QqGatewayTransport
from witty_service.channels.contracts import (
    CERTAINTY_REJECTED,
    CERTAINTY_UNCERTAIN,
    CONVERSATION_TYPE_DIRECT,
    CONVERSATION_TYPE_GROUP,
    DeliveryResult,
    InboundMessage,
    Route,
)
from witty_service.channels.delivery import plan

INSTANCE_ID = "instance-1"

#: 录制帧：私聊文本（字段名与官方网关 `C2C_MESSAGE_CREATE.d` 一致）
C2C_FRAME = {
    "id": "EVENT_1",
    "op": 0,
    "t": EVENT_C2C_MESSAGE_CREATE,
    "d": {
        "id": "MSG_1",
        "content": "帮我看看这个报错",
        "timestamp": "2026-09-13T10:00:00+08:00",
        "author": {"user_openid": "OPENID_1"},
    },
}

#: 录制帧：群里 @机器人（正文不含 @，@ 由 mentions 单独承载）
GROUP_FRAME = {
    "id": "EVENT_2",
    "op": 0,
    "t": EVENT_GROUP_AT_MESSAGE_CREATE,
    "d": {
        "id": "MSG_2",
        "content": "大家好",
        "timestamp": "1767225600000",
        "group_openid": "GROUP_1",
        "author": {"member_openid": "MEMBER_1"},
        "mentions": [{"id": "BOT_1", "is_you": True}],
    },
}


def _fresh(frame: dict) -> dict:
    """把录制帧的 timestamp 换成"刚刚"：出站用例关心的是被动回复窗口。"""
    return {**frame, "d": {**frame["d"], "timestamp": datetime.now(UTC).isoformat()}}


def _c2c_route() -> Route:
    return Route(
        instance_id=INSTANCE_ID,
        conversation_type=CONVERSATION_TYPE_DIRECT,
        platform_user_id="OPENID_1",
    )


def _group_route() -> Route:
    """群聊路由：会话归属看提问的成员，出站地址是 `group_openid`。"""
    return Route(
        instance_id=INSTANCE_ID,
        conversation_type=CONVERSATION_TYPE_GROUP,
        platform_user_id="MEMBER_1",
    )


# ================== 平台约束（照抄官方 SDK 的取值，改了这里就说明约束理解变了） ==================


def test_platform_limits_match_the_official_docs() -> None:
    assert MAX_TEXT_LENGTH == 4500  # Node SDK 的 DEFAULT_CHUNK_LIMIT
    # 条数上限取两种场景的下限：单聊 4 条 / 群聊 5 条（官方文档·频率与时效规则）
    assert MAX_REPLY_SEGMENTS == 4
    assert PASSIVE_REPLY_RULES[CONVERSATION_TYPE_DIRECT] == (3600.0, 4)
    assert PASSIVE_REPLY_RULES[CONVERSATION_TYPE_GROUP] == (300.0, 5)
    assert passive_reply_rule("unknown") == PASSIVE_REPLY_FALLBACK_RULE
    assert PASSIVE_REPLY_FALLBACK_RULE == (300.0, 4)


# ====================================== 归一化 ======================================


def test_normalize_c2c_message() -> None:
    message = normalize_event(C2C_FRAME, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.platform_event_id == "MSG_1"
    assert message.text == "帮我看看这个报错"
    assert message.unsupported_kind is None
    assert message.route.instance_id == INSTANCE_ID
    assert message.route.conversation_type == CONVERSATION_TYPE_DIRECT
    assert message.route.platform_user_id == "OPENID_1"
    assert message.received_at == datetime.fromisoformat("2026-09-13T10:00:00+08:00")


def test_normalize_group_message_keeps_the_sender_and_the_group() -> None:
    message = normalize_event(GROUP_FRAME, instance_id=INSTANCE_ID)

    assert message is not None
    # 群消息正文里没有 @机器人（@ 在 mentions 里），所以不需要像企微那样剥离
    assert message.text == "大家好"
    assert message.route.conversation_type == CONVERSATION_TYPE_GROUP
    assert message.route.platform_user_id == "MEMBER_1"
    # 出站地址是 group_openid：路由键放不下它，因此与消息一起单独取出
    assert outbound_target(GROUP_FRAME) == "GROUP_1"
    assert outbound_target(C2C_FRAME) == "OPENID_1"


@pytest.mark.parametrize(
    "frame",
    [
        {"id": "E", "t": "AT_MESSAGE_CREATE", "d": {}},  # 频道消息不在本期范围
        {"id": "E", "t": "DIRECT_MESSAGE_CREATE", "d": {}},
        {"id": "E", "t": "READY", "d": {}},
        {"id": "E", "op": 0},
        {"id": "E", "t": EVENT_C2C_MESSAGE_CREATE},
        {"id": "E", "t": EVENT_C2C_MESSAGE_CREATE, "d": "not-an-object"},
    ],
)
def test_non_message_events_are_ignored(frame: dict) -> None:
    assert normalize_event(frame, instance_id=INSTANCE_ID) is None


def test_missing_message_id_falls_back_to_event_id_then_unique() -> None:
    without_message_id = {
        "id": "EVENT_9",
        "t": EVENT_C2C_MESSAGE_CREATE,
        "d": {"content": "hi", "author": {"user_openid": "OPENID_1"}},
    }
    message = normalize_event(without_message_id, instance_id=INSTANCE_ID)
    assert message is not None
    assert message.platform_event_id == "EVENT_9"

    without_any_id = {
        "t": EVENT_C2C_MESSAGE_CREATE,
        "d": {"content": "hi", "author": {"user_openid": "OPENID_1"}},
    }
    first = normalize_event(without_any_id, instance_id=INSTANCE_ID)
    second = normalize_event(without_any_id, instance_id=INSTANCE_ID)
    assert first is not None and second is not None
    assert first.platform_event_id.startswith("fallback-")
    assert first.platform_event_id != second.platform_event_id


@pytest.mark.parametrize(
    ("content_type", "kind"),
    [
        ("image/png", "image"),
        ("voice", "voice"),
        ("audio/wav", "voice"),
        ("file", "file"),
        ("application/octet-stream", "unknown"),
    ],
)
def test_attachment_only_message_is_reported_as_unsupported(
    content_type: str, kind: str
) -> None:
    """不支持的内容**也要上报**（text=None + kind），降级文案只有一处。"""
    frame = {
        "id": "EVENT_3",
        "t": EVENT_C2C_MESSAGE_CREATE,
        "d": {
            "id": "MSG_3",
            "content": "",
            "author": {"user_openid": "OPENID_1"},
            "attachments": [{"content_type": content_type, "url": "https://x/1"}],
        },
    }

    message = normalize_event(frame, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.text is None
    assert message.unsupported_kind == kind


def test_voice_uses_the_platform_transcript() -> None:
    """平台给了 ASR 转写就当文本处理，不再自己下语音文件。"""
    frame = {
        "id": "EVENT_4",
        "t": EVENT_C2C_MESSAGE_CREATE,
        "d": {
            "id": "MSG_4",
            "content": "",
            "author": {"user_openid": "OPENID_1"},
            "attachments": [
                {
                    "content_type": "voice",
                    "voice_wav_url": "https://x/1.wav",
                    "asr_refer_text": "帮我看下构建日志",
                }
            ],
        },
    }

    message = normalize_event(frame, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.text == "帮我看下构建日志"
    assert message.unsupported_kind is None


def test_empty_message_without_attachments_is_reported_as_empty_text() -> None:
    frame = {
        "id": "EVENT_5",
        "t": EVENT_C2C_MESSAGE_CREATE,
        "d": {"id": "MSG_5", "content": "   ", "author": {"user_openid": "OPENID_1"}},
    }

    message = normalize_event(frame, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.text == ""
    assert message.unsupported_kind is None


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        ("1767225600000", datetime.fromtimestamp(1767225600, tz=UTC)),
        (1767225600000, datetime.fromtimestamp(1767225600, tz=UTC)),
        (
            "2026-09-13T10:00:00+08:00",
            datetime.fromisoformat("2026-09-13T10:00:00+08:00"),
        ),
        ("2026-09-13T02:00:00Z", datetime(2026, 9, 13, 2, 0, tzinfo=UTC)),
    ],
)
def test_timestamp_forms_are_normalized(timestamp: object, expected: datetime) -> None:
    frame = {
        "id": "EVENT_6",
        "t": EVENT_C2C_MESSAGE_CREATE,
        "d": {
            "id": "MSG_6",
            "content": "hi",
            "timestamp": timestamp,
            "author": {"user_openid": "OPENID_1"},
        },
    }

    message = normalize_event(frame, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.received_at == expected


@pytest.mark.parametrize("timestamp", ["", "not-a-time", None, {}])
def test_unparsable_timestamp_falls_back_to_now(timestamp: object) -> None:
    frame = {
        "id": "EVENT_7",
        "t": EVENT_C2C_MESSAGE_CREATE,
        "d": {
            "id": "MSG_7",
            "content": "hi",
            "timestamp": timestamp,
            "author": {"user_openid": "OPENID_1"},
        },
    }

    message = normalize_event(frame, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.received_at is not None
    assert abs((datetime.now(UTC) - message.received_at).total_seconds()) < 5


# ============================== 出站与生命周期：假传输层 ==============================


class FakeQqTransport:
    """假传输：记录每一次出站，并允许测试把录制帧推进来。"""

    def __init__(
        self,
        *,
        response: dict | None = None,
        error: BaseException | None = None,
        errors: list[BaseException] | None = None,
    ) -> None:
        self.connected = False
        self.closed = False
        self.sent: list[dict] = []
        self._handler = None
        self._response = {"id": "MSG_OUT_1"} if response is None else response
        self._error = error
        #: 按顺序消耗的失败脚本：先失败 n 次、之后恢复成功，用来验证额度记账
        self._errors = list(errors or [])

    def on_event(self, handler) -> None:
        self._handler = handler

    async def connect(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True

    def is_alive(self) -> bool:
        return self.connected and not self.closed

    async def send_message(self, **kwargs: object) -> dict | None:
        self.sent.append(dict(kwargs))
        if self._errors:
            raise self._errors.pop(0)
        if self._error is not None:
            raise self._error
        return self._response

    def push(self, frame: dict) -> None:
        assert self._handler is not None
        self._handler(frame)


def _adapter(transport: FakeQqTransport, **kwargs: object) -> QqBotAdapter:
    return QqBotAdapter(
        instance_id=INSTANCE_ID,
        config={"app_id": "102123456"},
        credentials={"secret": "s3cr3t"},
        transport_factory=lambda _app_id, _secret: transport,
        **kwargs,  # type: ignore[arg-type]
    )


async def _drain(adapter: QqBotAdapter) -> None:
    """等入站帧真的走到管线里（适配器把分发挂在任务上，不能假设立刻完成）。"""
    await adapter.wait_for_inbound()


@pytest.mark.asyncio
async def test_capabilities_are_declared_after_connect() -> None:
    transport = FakeQqTransport()
    adapter = _adapter(transport)

    await adapter.start()
    capabilities = adapter.capabilities()
    await adapter.stop()

    assert capabilities.can_edit_message is False
    assert capabilities.max_text_length == 4500
    assert capabilities.max_reply_segments == 4


@pytest.mark.asyncio
async def test_long_answer_is_capped_at_four_segments_on_qq() -> None:
    """回归：QQ 上不超过 4 条，且末条是"完整结果请在控制台查看"的提示。

    用的是**适配器自己声明的能力**（不是测试里另写一份常量），适配器与分段规划的口径
    一旦漂移这条用例就红。
    """
    transport = FakeQqTransport()
    adapter = _adapter(transport)
    await adapter.start()
    capabilities = adapter.capabilities()
    await adapter.stop()

    # 40 段 ≈ 24k 字符：足够切出 4 段以上，才能验证"截断后末条是提示"
    result = plan(("段落。" * 200 + "\n\n") * 40, capabilities)

    assert capabilities.max_text_length == MAX_TEXT_LENGTH
    assert capabilities.max_reply_segments == MAX_REPLY_SEGMENTS
    assert len(result.actions) <= MAX_REPLY_SEGMENTS
    assert result.truncated is True
    assert result.actions[-1].is_console_notice is True
    for action in result.actions:
        assert len(action.text) <= MAX_TEXT_LENGTH


@pytest.mark.asyncio
async def test_lifecycle_connects_once_and_closes() -> None:
    transport = FakeQqTransport()
    adapter = _adapter(transport)

    await adapter.start()
    assert transport.connected is True
    assert adapter.is_alive() is True

    await adapter.stop()
    assert transport.closed is True
    assert adapter.is_alive() is False


@pytest.mark.asyncio
async def test_inbound_frame_is_emitted_to_the_pipeline() -> None:
    transport = FakeQqTransport()
    adapter = _adapter(transport)
    received: list[InboundMessage] = []

    async def handler(message: InboundMessage) -> None:
        received.append(message)

    adapter.on_inbound(handler)
    await adapter.start()
    transport.push(_fresh(C2C_FRAME))
    await _drain(adapter)

    assert len(received) == 1
    assert received[0].text == "帮我看看这个报错"
    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame",
    [
        # 群消息缺 group_openid：出站地址缺失
        {
            **GROUP_FRAME,
            "d": {**GROUP_FRAME["d"], "group_openid": ""},
        },
        # 私聊缺 author.user_openid：出站地址同样缺失
        {
            **C2C_FRAME,
            "d": {**C2C_FRAME["d"], "author": {}},
        },
    ],
)
async def test_message_without_an_outbound_target_is_dropped(frame: dict) -> None:
    """没有出站地址就**不回**：绝不退回落成"私聊发给提问的成员"。

    群聊的 route.platform_user_id 是成员的 member_openid，而 QQ 的出站地址必须是
    group_openid。退回前者会把回答私聊发给这个成员——是"发错地方"，比不回复严重
    得多（既不进群，又可能把只有群友该看的内容发进单聊）。
    """
    transport = FakeQqTransport()
    adapter = _adapter(transport)
    received: list[InboundMessage] = []

    async def handler(message: InboundMessage) -> None:
        received.append(message)

    adapter.on_inbound(handler)
    await adapter.start()

    transport.push(_fresh(frame))
    await _drain(adapter)
    assert received == []
    assert adapter._contexts == {}

    transport.push(_fresh(C2C_FRAME))
    await _drain(adapter)
    assert len(received) == 1
    await adapter.stop()


@pytest.mark.asyncio
async def test_frames_after_stop_are_dropped() -> None:
    transport = FakeQqTransport()
    adapter = _adapter(transport)
    received: list[InboundMessage] = []

    async def handler(message: InboundMessage) -> None:
        received.append(message)

    adapter.on_inbound(handler)
    await adapter.start()
    await adapter.stop()
    transport.push(_fresh(C2C_FRAME))
    await _drain(adapter)

    assert received == []


@pytest.mark.asyncio
async def test_send_text_uses_a_passive_reply_with_msg_id_and_increasing_seq() -> None:
    transport = FakeQqTransport()
    adapter = _adapter(transport)
    await adapter.start()
    transport.push(_fresh(C2C_FRAME))
    await _drain(adapter)

    first = await adapter.send_text(_c2c_route(), "第一条")
    second = await adapter.send_text(_c2c_route(), "第二条")

    assert first == DeliveryResult.delivered_with("MSG_OUT_1")
    assert transport.sent[0]["msg_id"] == "MSG_1"
    assert transport.sent[0]["msg_seq"] == 1
    # 平台要求 msg_id + msg_seq 唯一：同一个 msg_id 下必须递增
    assert transport.sent[1]["msg_id"] == "MSG_1"
    assert transport.sent[1]["msg_seq"] == 2
    assert transport.sent[0]["target_id"] == "OPENID_1"
    assert transport.sent[0]["markdown"] is False
    assert second.delivered is True
    await adapter.stop()


@pytest.mark.asyncio
async def test_passive_budget_exhausted_degrades_to_proactive() -> None:
    """额度用尽是**本地可判定**的确定性前提：改用主动消息，而不是撞平台限制。"""
    transport = FakeQqTransport()
    adapter = _adapter(transport)
    await adapter.start()
    transport.push(_fresh(C2C_FRAME))
    await _drain(adapter)

    budget = PASSIVE_REPLY_RULES[CONVERSATION_TYPE_DIRECT][1]
    for index in range(budget + 1):
        await adapter.send_text(_c2c_route(), f"第 {index} 条")

    passive = [sent for sent in transport.sent if sent["msg_id"] is not None]
    proactive = [sent for sent in transport.sent if sent["msg_id"] is None]
    assert len(passive) == budget
    assert len(proactive) == 1
    # 主动消息体两个字段都不带（官方 buildProactiveBody）
    assert proactive[0]["msg_seq"] is None
    await adapter.stop()


@pytest.mark.asyncio
async def test_within_budget_stays_passive() -> None:
    """降级点的另一条：额度未用尽、窗口未过期时**不得**提前退化。"""
    transport = FakeQqTransport()
    adapter = _adapter(transport)
    await adapter.start()
    transport.push(_fresh(C2C_FRAME))
    await _drain(adapter)

    for index in range(PASSIVE_REPLY_RULES[CONVERSATION_TYPE_DIRECT][1]):
        await adapter.send_text(_c2c_route(), f"第 {index} 条")

    assert all(sent["msg_id"] == "MSG_1" for sent in transport.sent)
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_budget_is_five_while_direct_is_four() -> None:
    """条数按场景不同（官方文档）：群聊 5 条、单聊 4 条；共用一份会白白少发一条。"""
    transport = FakeQqTransport()
    adapter = _adapter(transport)
    await adapter.start()
    transport.push(_fresh(GROUP_FRAME))
    await _drain(adapter)

    for index in range(5):
        await adapter.send_text(_group_route(), f"第 {index} 条")

    passive = [sent for sent in transport.sent if sent["msg_id"] is not None]
    assert len(passive) == 5
    assert [sent["msg_seq"] for sent in passive] == [1, 2, 3, 4, 5]
    await adapter.stop()


@pytest.mark.asyncio
async def test_redelivered_message_does_not_reset_the_passive_budget() -> None:
    """平台会重复推送同一条 `msg_id`：**不得**重置计数与 `msg_seq`，否则会被判重拒绝。"""
    transport = FakeQqTransport()
    adapter = _adapter(transport)
    await adapter.start()
    transport.push(_fresh(C2C_FRAME))
    await _drain(adapter)
    for index in range(3):
        await adapter.send_text(_c2c_route(), f"第 {index} 条")

    transport.push(_fresh(C2C_FRAME))
    await _drain(adapter)
    await adapter.send_text(_c2c_route(), "第 4 条")

    assert [sent["msg_seq"] for sent in transport.sent] == [1, 2, 3, 4]
    assert all(sent["msg_id"] == "MSG_1" for sent in transport.sent)
    await adapter.stop()


@pytest.mark.asyncio
async def test_an_uncertain_send_still_consumes_the_passive_quota() -> None:
    """结果不确定（超时 / 5xx）同样要扣额度：分界是"平台是否可能已经计入"。

    只看"本地是否成功"的话，同一条入站消息会发出第 5 条被动回复并被平台拒绝。
    """
    timeout = QqTransportError(
        err.CHANNEL_DELIVERY_UNCERTAIN, "timeout", certainty=CERTAINTY_UNCERTAIN
    )
    transport = FakeQqTransport(errors=[timeout] * MAX_REPLY_SEGMENTS)
    adapter = _adapter(transport)
    await adapter.start()
    transport.push(_fresh(C2C_FRAME))
    await _drain(adapter)

    for index in range(MAX_REPLY_SEGMENTS):
        result = await adapter.send_text(_c2c_route(), f"第 {index} 条")
        assert result.certainty == CERTAINTY_UNCERTAIN

    await adapter.send_text(_c2c_route(), "第 5 条")

    assert [sent["msg_id"] for sent in transport.sent] == ["MSG_1"] * 4 + [None]
    await adapter.stop()


@pytest.mark.asyncio
async def test_a_rejected_send_does_not_consume_the_passive_quota() -> None:
    """平台**明确拒绝**不算额度：请求根本没被受理，也就没进平台的账本。

    与上一条合起来就是记账的完整规则：按三态分界，而不是按"成功 / 失败"。
    `msg_seq` 仍然逐次递增——同一个 `msg_id + msg_seq` 重复发才会被平台判重。
    """
    refused = QqTransportError("QQ_403", "no permission", certainty=CERTAINTY_REJECTED)
    transport = FakeQqTransport(errors=[refused] * MAX_REPLY_SEGMENTS)
    adapter = _adapter(transport)
    await adapter.start()
    transport.push(_fresh(C2C_FRAME))
    await _drain(adapter)

    for index in range(MAX_REPLY_SEGMENTS):
        assert (await adapter.send_text(_c2c_route(), f"第 {index} 条")).rejected

    await adapter.send_text(_c2c_route(), "第 5 条")

    last = transport.sent[-1]
    assert last["msg_id"] == "MSG_1"
    assert last["msg_seq"] == MAX_REPLY_SEGMENTS + 1
    await adapter.stop()


@pytest.mark.asyncio
async def test_expired_window_degrades_to_proactive() -> None:
    """被动回复必须基于窗口内的入站消息（单聊 60 分钟）：过期后改用主动消息。"""
    transport = FakeQqTransport()
    adapter = _adapter(transport)
    await adapter.start()
    stale = {
        "id": "EVENT_OLD",
        "t": EVENT_C2C_MESSAGE_CREATE,
        "d": {
            "id": "MSG_OLD",
            "content": "很久以前的消息",
            "timestamp": (
                datetime.now(UTC)
                - timedelta(
                    seconds=passive_reply_rule(CONVERSATION_TYPE_DIRECT)[0] + 60
                )
            ).isoformat(),
            "author": {"user_openid": "OPENID_1"},
        },
    }
    transport.push(stale)
    await _drain(adapter)

    await adapter.send_text(_c2c_route(), "结果")

    assert transport.sent[0]["msg_id"] is None
    assert transport.sent[0]["target_id"] == "OPENID_1"
    await adapter.stop()


@pytest.mark.asyncio
async def test_without_any_inbound_message_sends_proactively() -> None:
    transport = FakeQqTransport()
    adapter = _adapter(transport)
    await adapter.start()

    result = await adapter.send_text(_c2c_route(), "连通性测试")

    assert result.delivered is True
    assert transport.sent[0]["msg_id"] is None
    await adapter.stop()


@pytest.mark.asyncio
async def test_group_reply_targets_the_group_openid() -> None:
    transport = FakeQqTransport()
    adapter = _adapter(transport)
    await adapter.start()
    transport.push(_fresh(GROUP_FRAME))
    await _drain(adapter)
    group_route = Route(
        instance_id=INSTANCE_ID,
        conversation_type=CONVERSATION_TYPE_GROUP,
        platform_user_id="MEMBER_1",
    )

    await adapter.send_text(group_route, "结果")

    assert transport.sent[0]["conversation_type"] == CONVERSATION_TYPE_GROUP
    assert transport.sent[0]["target_id"] == "GROUP_1"
    await adapter.stop()


@pytest.mark.asyncio
async def test_uncertain_result_is_not_retried() -> None:
    """结果不确定（SDK 吞掉超时并返回 None）时**不重发、不降级**。"""
    transport = FakeQqTransport(
        error=QqTransportError(
            err.CHANNEL_DELIVERY_UNCERTAIN, "no response", certainty=CERTAINTY_UNCERTAIN
        )
    )
    adapter = _adapter(transport)
    await adapter.start()

    result = await adapter.send_text(_c2c_route(), "结果")

    assert result.certainty == CERTAINTY_UNCERTAIN
    assert result.error_code == err.CHANNEL_DELIVERY_UNCERTAIN
    assert len(transport.sent) == 1
    await adapter.stop()


@pytest.mark.asyncio
async def test_platform_rejection_is_reported_as_rejected() -> None:
    transport = FakeQqTransport(
        error=QqTransportError("QQ_403", "no permission", certainty=CERTAINTY_REJECTED)
    )
    adapter = _adapter(transport)
    await adapter.start()

    result = await adapter.send_text(_c2c_route(), "结果")

    assert result.certainty == CERTAINTY_REJECTED
    assert result.error_code == "QQ_403"
    await adapter.stop()


@pytest.mark.asyncio
async def test_unknown_exception_from_the_transport_is_uncertain() -> None:
    transport = FakeQqTransport(error=ValueError("??"))
    adapter = _adapter(transport)
    await adapter.start()

    result = await adapter.send_text(_c2c_route(), "结果")

    assert result.certainty == CERTAINTY_UNCERTAIN
    assert result.error_code == err.CHANNEL_DELIVERY_UNCERTAIN
    await adapter.stop()


@pytest.mark.asyncio
async def test_markdown_is_opt_in() -> None:
    """`markdownSupport` 在官方 SDK 里默认 false：没有权限时发 markdown 会被拒。"""
    plain_transport = FakeQqTransport()
    plain = _adapter(plain_transport)
    markdown_transport = FakeQqTransport()
    markdown = QqBotAdapter(
        instance_id=INSTANCE_ID,
        config={"app_id": "102123456", "use_markdown": "true"},
        credentials={"secret": "s3cr3t"},
        transport_factory=lambda _app_id, _secret: markdown_transport,
    )

    await plain.start()
    await plain.send_text(_c2c_route(), "结果")
    await plain.stop()
    await markdown.start()
    await markdown.send_text(_c2c_route(), "结果")
    await markdown.stop()

    assert plain_transport.sent[0]["markdown"] is False
    assert markdown_transport.sent[0]["markdown"] is True


def test_default_transport_is_the_native_gateway() -> None:
    """不注入工厂时的默认装配必须落到 `qq_transport.QqGatewayTransport`。"""
    transport = build_native_transport("102123456", "s3cr3t", False, 5.0)

    assert isinstance(transport, QqGatewayTransport)
    assert transport.is_alive() is False


@pytest.mark.asyncio
async def test_instance_config_reaches_the_default_transport(monkeypatch) -> None:
    """实例配置里的 sandbox 必须传给传输层——配了沙箱却连生产域名是最难查的一类错。"""
    captured: dict[str, Any] = {}

    class _Recorder:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def on_event(self, handler: object) -> None: ...

        async def connect(self) -> None: ...

        async def close(self) -> None: ...

        def is_alive(self) -> bool:
            return True

        async def send_message(self, **kwargs: Any) -> dict[str, Any]:
            return {"id": "MSG_1"}

    monkeypatch.setattr(qq_bot, "QqGatewayTransport", _Recorder)
    adapter = QqBotAdapter(
        instance_id=INSTANCE_ID,
        config={"app_id": "102123456", "sandbox": "true"},
        credentials={"secret": "s3cr3t"},
    )

    await adapter.start()

    assert captured["app_id"] == "102123456"
    assert captured["secret"] == "s3cr3t"
    assert captured["sandbox"] is True
    await adapter.stop()


def test_credential_split_keeps_the_secret_out_of_config() -> None:
    material = QqBotAdapter.split_credentials(
        {
            "app_id": "102123456",
            "secret": "s3cr3t",
            "sandbox": "true",
            # 扫码路径写进来的扫码人 openid：非密，且要能在实例详情里看到
            "owner_user_openid": "OPENID_1",
        }
    )

    assert material.config == {
        "app_id": "102123456",
        "sandbox": "true",
        "owner_user_openid": "OPENID_1",
    }
    assert material.secrets == {"secret": "s3cr3t"}
    assert material.mask == "1021****3456"


def test_required_credentials_are_declared() -> None:
    assert QqBotAdapter.required_credentials == ("app_id", "secret")
    assert [field.name for field in QqBotAdapter.credential_fields] == [
        "app_id",
        "secret",
        "use_markdown",
        "sandbox",
    ]
