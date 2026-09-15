"""W12 企微适配器的协议、归一化与三态映射测试。

协议事实来自官方 Node SDK `@wecom/aibot-node-sdk@1.0.7` 与参考实现 dsh-im
（见 `adapters/wecom_bot.py` 的模块 docstring），因此这里断言的是**线格式**：
帧 cmd 取值域、回执帧没有 cmd 且靠 req_id 前缀关联、入站字段名、单聊 chatid = userid。
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
import websockets.exceptions

from witty_service.channels import errors as err
from witty_service.channels.adapters.wecom_bot import (
    DEFAULT_WS_URL,
    WS_CMD_HEARTBEAT,
    WS_CMD_MSG_CALLBACK,
    WS_CMD_SEND,
    WS_CMD_SUBSCRIBE,
    WecomBotAdapter,
    WecomProtocolError,
    build_heartbeat_frame,
    build_req_id,
    build_respond_frame,
    build_send_frame,
    build_subscribe_frame,
    is_ack_frame,
    normalize_callback,
)
from witty_service.channels.contracts import (
    CERTAINTY_DELIVERED,
    CERTAINTY_REJECTED,
    CERTAINTY_UNCERTAIN,
    CONVERSATION_TYPE_DIRECT,
    CONVERSATION_TYPE_GROUP,
    DeliveryResult,
    InboundMessage,
    Route,
)

INSTANCE_ID = "instance-1"


def _route() -> Route:
    return Route(
        instance_id=INSTANCE_ID, conversation_type="direct", platform_user_id="user-1"
    )


#: 录制帧：单聊文本推送（字段名与官方协议一致）
TEXT_CALLBACK = {
    "cmd": WS_CMD_MSG_CALLBACK,
    "headers": {"req_id": "aibot_msg_callback_1"},
    "body": {
        "msgid": "msg-1001",
        "chattype": "single",
        "msgtype": "text",
        "text": {"content": "帮我看看这个报错"},
        "from": {"userid": "zhangsan"},
        "create_time": 1767225600,
    },
}

GROUP_CALLBACK = {
    "cmd": WS_CMD_MSG_CALLBACK,
    "headers": {"req_id": "aibot_msg_callback_2"},
    "body": {
        "msgid": "msg-1002",
        "chattype": "group",
        "chatid": "chat-1",
        "msgtype": "text",
        "text": {"content": "@机器人 大家好"},
        "from": {"userid": "zhangsan"},
    },
}

EVENT_CALLBACK = {
    "cmd": "aibot_event_callback",
    "headers": {"req_id": "aibot_event_callback_1"},
    "body": {
        "msgid": "evt-1",
        "msgtype": "event",
        "event": {"eventtype": "disconnected_event"},
    },
}


# ==============================================================================
# 协议常量与帧构造（线格式）
# ==============================================================================


def test_ws_url_matches_the_official_sdk() -> None:
    """官方 SDK 的 `DEFAULT_WS_URL`；早期猜测的 qyapi 地址是错的。"""
    assert DEFAULT_WS_URL == "wss://openws.work.weixin.qq.com"


def test_frame_builders_match_the_official_protocol() -> None:
    subscribe = build_subscribe_frame("bot-1", "secret-1", req_id="r1")
    assert subscribe == {
        "cmd": WS_CMD_SUBSCRIBE,
        "headers": {"req_id": "r1"},
        "body": {"bot_id": "bot-1", "secret": "secret-1"},
    }

    heartbeat = build_heartbeat_frame(req_id="r2")
    assert heartbeat["cmd"] == WS_CMD_HEARTBEAT == "ping"
    assert heartbeat["headers"]["req_id"] == "r2"

    send = build_send_frame(req_id="r3", chat_id="user-1", text="结果")
    assert send["cmd"] == WS_CMD_SEND == "aibot_send_msg"
    assert send["body"] == {
        "chatid": "user-1",
        "msgtype": "markdown",
        "markdown": {"content": "结果"},
    }

    respond = build_respond_frame(req_id="r4", text="结果", stream_id="s4")
    assert respond["cmd"] == "aibot_respond_msg"
    assert respond["body"]["msgtype"] == "stream"
    assert respond["body"]["stream"] == {
        "id": "s4",
        "finish": True,
        "content": "结果",
    }


def test_proactive_send_never_uses_the_text_msgtype() -> None:
    """回归：真机上 `aibot_send_msg` + `msgtype:"text"` 被平台判 40008（不合法的消息类型）。

    合法取值来自官方 SDK 的穷举类型 `SendMsgBody = SendMarkdownMsgBody |
    SendTemplateCardMsgBody | SendMediaMsgBody`（dist/index.d.ts:600）；`text` 只属于
    入站回调与欢迎语被动回复。这里锁住类型取值域，防止再被"入站字段名"带偏。
    """
    body = build_send_frame(req_id="r3", chat_id="user-1", text="结果")["body"]
    assert body["msgtype"] in {"markdown", "template_card", "image", "file", "video", "voice"}
    assert body["msgtype"] != "text"
    assert "text" not in body


def test_req_id_is_prefixed_by_command() -> None:
    """平台靠 req_id **前缀**区分回执类型（认证/心跳/发送）。"""
    assert build_req_id(WS_CMD_SUBSCRIBE).startswith(f"{WS_CMD_SUBSCRIBE}_")
    assert build_req_id(WS_CMD_SEND).startswith(f"{WS_CMD_SEND}_")


@pytest.mark.parametrize(
    ("frame", "expected"),
    [
        ({"headers": {"req_id": "aibot_send_msg_1"}, "errcode": 0}, True),
        ({"headers": {"req_id": "ping_1"}, "errcode": 0, "errmsg": "ok"}, True),
        ({"cmd": WS_CMD_MSG_CALLBACK, "headers": {"req_id": "x"}}, False),
        ({"errcode": 0}, False),
    ],
)
def test_ack_frame_detection(frame: dict, expected: bool) -> None:
    assert is_ack_frame(frame) is expected


# ==============================================================================
# 归一化
# ==============================================================================


def test_normalize_text_callback() -> None:
    message = normalize_callback(TEXT_CALLBACK, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.platform_event_id == "msg-1001"
    assert message.text == "帮我看看这个报错"
    assert message.unsupported_kind is None
    assert message.route.instance_id == INSTANCE_ID
    assert message.route.conversation_type == CONVERSATION_TYPE_DIRECT
    assert message.route.platform_user_id == "zhangsan"
    assert message.received_at == datetime.fromtimestamp(1767225600, tz=UTC)


def test_normalize_group_callback_strips_mention() -> None:
    message = normalize_callback(GROUP_CALLBACK, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.route.conversation_type == CONVERSATION_TYPE_GROUP
    # 群里的 @机器人 是路由信息，不属于用户的问题
    assert message.text == "大家好"


def test_normalize_voice_uses_platform_transcript() -> None:
    frame = {
        "cmd": WS_CMD_MSG_CALLBACK,
        "headers": {"req_id": "r"},
        "body": {
            "msgid": "msg-voice",
            "chattype": "single",
            "msgtype": "voice",
            "voice": {"content": "帮我看下构建日志"},
            "from": {"userid": "zhangsan"},
        },
    }

    message = normalize_callback(frame, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.text == "帮我看下构建日志"
    assert message.unsupported_kind is None


def test_normalize_voice_without_transcript_is_degraded() -> None:
    frame = {
        "cmd": WS_CMD_MSG_CALLBACK,
        "headers": {"req_id": "r"},
        "body": {
            "msgid": "msg-voice-2",
            "chattype": "single",
            "msgtype": "voice",
            "voice": {"media_id": "m1"},
            "from": {"userid": "zhangsan"},
        },
    }

    message = normalize_callback(frame, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.text is None
    assert message.unsupported_kind == "voice"


def test_normalize_mixed_joins_text_items() -> None:
    frame = {
        "cmd": WS_CMD_MSG_CALLBACK,
        "headers": {"req_id": "r"},
        "body": {
            "msgid": "msg-mixed",
            "chattype": "single",
            "msgtype": "mixed",
            "mixed": {
                "msg_item": [
                    {"msgtype": "text", "text": {"content": "第一段"}},
                    {"msgtype": "image", "image": {"media_id": "i1"}},
                    {"msgtype": "text", "text": {"content": "第二段"}},
                ]
            },
            "from": {"userid": "zhangsan"},
        },
    }

    message = normalize_callback(frame, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.text == "第一段\n第二段"


def test_normalize_mixed_without_text_is_degraded_as_image() -> None:
    frame = {
        "cmd": WS_CMD_MSG_CALLBACK,
        "headers": {"req_id": "r"},
        "body": {
            "msgid": "msg-mixed-2",
            "chattype": "single",
            "msgtype": "mixed",
            "mixed": {"msg_item": [{"msgtype": "image", "image": {"media_id": "i1"}}]},
            "from": {"userid": "zhangsan"},
        },
    }

    message = normalize_callback(frame, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.text is None
    assert message.unsupported_kind == "image"


@pytest.mark.parametrize(
    ("msgtype", "kind"),
    [("image", "image"), ("file", "file"), ("video", "file"), ("sticker", "unknown")],
)
def test_normalize_unsupported_content_is_still_reported(msgtype: str, kind: str) -> None:
    """不支持的内容也要上报（text=None + kind），降级文案只有一处。"""
    frame = {
        "cmd": WS_CMD_MSG_CALLBACK,
        "headers": {"req_id": "r"},
        "body": {
            "msgid": f"msg-{msgtype}",
            "chattype": "single",
            "msgtype": msgtype,
            "from": {"userid": "zhangsan"},
        },
    }

    message = normalize_callback(frame, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.text is None
    assert message.unsupported_kind == kind


@pytest.mark.parametrize(
    "frame",
    [
        {"headers": {"req_id": "r"}, "errcode": 0},  # 回执帧
        EVENT_CALLBACK,  # 事件推送不是用户消息
        {"cmd": WS_CMD_MSG_CALLBACK},
        {"cmd": WS_CMD_MSG_CALLBACK, "body": "not-an-object"},
    ],
)
def test_non_message_frames_are_ignored(frame: dict) -> None:
    assert normalize_callback(frame, instance_id=INSTANCE_ID) is None


def test_missing_msgid_falls_back_to_req_id_then_unique() -> None:
    with_req_id = {
        "cmd": WS_CMD_MSG_CALLBACK,
        "headers": {"req_id": "aibot_msg_callback_9"},
        "body": {"chattype": "single", "msgtype": "text", "text": {"content": "hi"}},
    }
    message = normalize_callback(with_req_id, instance_id=INSTANCE_ID)
    assert message is not None
    assert message.platform_event_id == "aibot_msg_callback_9"

    without_any = {
        "cmd": WS_CMD_MSG_CALLBACK,
        "headers": {},
        "body": {"chattype": "single", "msgtype": "text", "text": {"content": "hi"}},
    }
    first = normalize_callback(without_any, instance_id=INSTANCE_ID)
    second = normalize_callback(without_any, instance_id=INSTANCE_ID)
    assert first is not None and second is not None
    assert first.platform_event_id.startswith("fallback-")
    assert first.platform_event_id != second.platform_event_id


def test_millisecond_create_time_is_normalized() -> None:
    frame = json.loads(json.dumps(TEXT_CALLBACK))
    frame["body"]["create_time"] = 1767225600000

    message = normalize_callback(frame, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.received_at == datetime.fromtimestamp(1767225600, tz=UTC)


def test_missing_create_time_falls_back_to_now() -> None:
    frame = json.loads(json.dumps(TEXT_CALLBACK))
    del frame["body"]["create_time"]

    message = normalize_callback(frame, instance_id=INSTANCE_ID)

    assert message is not None
    assert message.received_at is not None
    assert abs((datetime.now(UTC) - message.received_at).total_seconds()) < 5


# ==============================================================================
# 三态映射
# ==============================================================================


def _adapter(**kwargs: object) -> WecomBotAdapter:
    return WecomBotAdapter(
        instance_id=INSTANCE_ID,
        config={"bot_id": "bot-12345678"},
        credentials={"secret": "s3cr3t"},
        **kwargs,  # type: ignore[arg-type]
    )


def test_credential_error_is_rejected() -> None:
    result = _adapter().classify_exception(WecomProtocolError("40001", "invalid secret"))

    assert result.certainty == CERTAINTY_REJECTED
    assert result.error_code == err.CHANNEL_CREDENTIAL_INVALID


def test_unknown_platform_error_is_uncertain() -> None:
    """**代价不对称**：只有已知的凭据类错误才算确定性拒绝，其余一律 uncertain。"""
    result = _adapter().classify_exception(WecomProtocolError("45009", "rate limited"))

    assert result.certainty == CERTAINTY_UNCERTAIN
    assert result.error_code == "WECOM_45009"


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("socket timeout"),
        ConnectionError("connection reset"),
        websockets.exceptions.ConnectionClosedError(None, None),
        ValueError("??"),
    ],
)
def test_socket_and_unknown_problems_are_uncertain(exc: BaseException) -> None:
    """超时/断连/未知异常 -> uncertain：**不重发**，否则用户会收到两条。"""
    result = _adapter().classify_exception(exc)

    assert result.certainty == CERTAINTY_UNCERTAIN
    assert result.error_code == err.CHANNEL_DELIVERY_UNCERTAIN


# ==============================================================================
# 出站与生命周期：假传输层
# ==============================================================================


class FakeWecomTransport:
    """假传输：记录发出的帧，并把**没有 cmd 的回执帧**回灌给接收循环。"""

    def __init__(
        self,
        *,
        reply_body: dict | None = None,
        ack: bool = True,
        ack_errcode: int = 0,
    ) -> None:
        self.sent: list[dict] = []
        self.connected = False
        self.closed = False
        self._incoming: asyncio.Queue[dict] = asyncio.Queue()
        self._ack = ack
        self._ack_errcode = ack_errcode
        self._reply_body = reply_body if reply_body is not None else {"msgid": "m-1"}

    async def connect(self) -> None:
        self.connected = True

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)
        if not self._ack and payload.get("cmd") in (WS_CMD_SEND, WS_CMD_HEARTBEAT):
            return
        req_id = (payload.get("headers") or {}).get("req_id")
        frame: dict = {"headers": {"req_id": req_id}, "errcode": self._ack_errcode}
        if self._ack_errcode == 0:
            frame["errmsg"] = "ok"
            if payload.get("cmd") == WS_CMD_SEND:
                frame["body"] = self._reply_body
        else:
            frame["errmsg"] = "bad"
        self._incoming.put_nowait(frame)

    async def recv_json(self) -> dict:
        return await self._incoming.get()

    async def close(self) -> None:
        self.closed = True

    def push(self, frame: dict) -> None:
        self._incoming.put_nowait(frame)


@pytest.mark.asyncio
async def test_start_sends_subscribe_and_stop_closes() -> None:
    transport = FakeWecomTransport()
    adapter = _adapter(transport_factory=lambda _url: transport)

    await adapter.start()

    assert transport.connected is True
    assert transport.sent[0]["cmd"] == WS_CMD_SUBSCRIBE
    assert transport.sent[0]["body"] == {"bot_id": "bot-12345678", "secret": "s3cr3t"}
    assert adapter.is_alive() is True

    await adapter.stop()
    assert transport.closed is True
    assert adapter.is_alive() is False


@pytest.mark.asyncio
async def test_start_fails_when_auth_is_rejected() -> None:
    transport = FakeWecomTransport(ack_errcode=40001)
    adapter = _adapter(transport_factory=lambda _url: transport)

    with pytest.raises(WecomProtocolError):
        await adapter.start()

    assert transport.closed is True  # 认证失败要关掉连接，不留下半开的长连接
    assert adapter.is_alive() is False


@pytest.mark.asyncio
async def test_send_text_uses_proactive_send_with_chatid() -> None:
    transport = FakeWecomTransport(reply_body={"msgid": "m-42"})
    adapter = _adapter(transport_factory=lambda _url: transport)
    await adapter.start()

    result = await adapter.send_text(_route(), "结果")

    assert result.certainty == CERTAINTY_DELIVERED
    assert result.platform_message_ref == "m-42"
    sent = transport.sent[-1]
    assert sent["cmd"] == WS_CMD_SEND
    assert sent["body"]["chatid"] == "user-1"  # 单聊：chatid 就是 userid
    assert sent["body"]["msgtype"] == "markdown"
    assert sent["body"]["markdown"]["content"] == "结果"
    await adapter.stop()


@pytest.mark.asyncio
async def test_send_text_is_uncertain_when_no_ack() -> None:
    transport = FakeWecomTransport(ack=False)
    adapter = _adapter(
        transport_factory=lambda _url: transport, ack_timeout_seconds=0.05
    )
    adapter._transport = transport  # 跳过鉴权，直接验证发送路径
    adapter._connected = True

    result = await adapter.send_text(_route(), "结果")

    assert result.certainty == CERTAINTY_UNCERTAIN
    assert len([f for f in transport.sent if f["cmd"] == WS_CMD_SEND]) == 1


@pytest.mark.asyncio
async def test_inbound_callback_is_emitted() -> None:
    transport = FakeWecomTransport()
    adapter = _adapter(transport_factory=lambda _url: transport)
    received: list[InboundMessage] = []

    async def handler(message: InboundMessage) -> None:
        received.append(message)

    adapter.on_inbound(handler)
    await adapter.start()
    transport.push(TEXT_CALLBACK)
    for _ in range(50):
        if received:
            break
        await asyncio.sleep(0.01)

    assert len(received) == 1
    assert received[0].text == "帮我看看这个报错"
    # 协议里回调不需要额外 ack（回复本身就是回执）：除订阅之外不发别的帧
    assert [f["cmd"] for f in transport.sent] == [WS_CMD_SUBSCRIBE]
    await adapter.stop()


@pytest.mark.asyncio
async def test_heartbeat_is_sent_periodically() -> None:
    transport = FakeWecomTransport()
    adapter = _adapter(
        transport_factory=lambda _url: transport, heartbeat_interval_seconds=0.02
    )
    await adapter.start()
    await asyncio.sleep(0.1)
    await adapter.stop()

    assert any(f["cmd"] == WS_CMD_HEARTBEAT for f in transport.sent)


@pytest.mark.asyncio
async def test_two_missed_heartbeats_mark_the_connection_dead() -> None:
    transport = FakeWecomTransport(ack=False)
    adapter = _adapter(
        transport_factory=lambda _url: transport, heartbeat_interval_seconds=0.01
    )
    await adapter.start()
    for _ in range(60):
        if not adapter.is_alive():
            break
        await asyncio.sleep(0.01)

    assert adapter.is_alive() is False
    assert transport.closed is True


@pytest.mark.asyncio
async def test_server_disconnect_event_ends_the_connection() -> None:
    transport = FakeWecomTransport()
    adapter = _adapter(transport_factory=lambda _url: transport)
    await adapter.start()

    transport.push(EVENT_CALLBACK)
    for _ in range(50):
        if not adapter.is_alive():
            break
        await asyncio.sleep(0.01)

    # 服务端说"已有新连接"，本连接不再自愈（由网关按退避决定何时重连）
    assert adapter.is_alive() is False
    await adapter.stop()


@pytest.mark.asyncio
async def test_capabilities_are_declared_conservatively() -> None:
    transport = FakeWecomTransport()
    adapter = _adapter(transport_factory=lambda _url: transport)

    await adapter.start()
    capabilities = adapter.capabilities()
    await adapter.stop()

    assert capabilities.can_edit_message is False
    assert capabilities.max_text_length == 2000
    assert capabilities.max_reply_segments is None


def test_delivered_result_uses_ack_reference() -> None:
    adapter = _adapter()
    frame = {"headers": {"req_id": "r"}, "errcode": 0, "body": {"msgid": "m-9"}}

    result = adapter._result_from_ack(frame)

    assert result == DeliveryResult.delivered_with("m-9")

# ==============================================================================
# 连接生命周期（真机 2026-09-13 事故的回归）
#
# 现场：实例在 UI 上永远显示"未连接"，网关日志每 15s 记一条"Channel instance connected"
# 却一次都没真正重连；同时进程里残留着两条指向 openws 的 socket，平台按"新连接建立"
# 规则反复踢掉旧连接。三个成因各自被下面三条测试锁住。
# ==============================================================================

TAKEOVER_EVENT = {
    "cmd": "aibot_event_callback",
    "headers": {"req_id": "aibot_event_callback_1"},
    "body": {
        "msgid": "evt-1",
        "msgtype": "event",
        "event": {"eventtype": "disconnected_event"},
    },
}


def _recording_factory() -> tuple[list[FakeWecomTransport], object]:
    transports: list[FakeWecomTransport] = []

    def factory(_url: str) -> FakeWecomTransport:
        transport = FakeWecomTransport()
        transports.append(transport)
        return transport

    return transports, factory


async def _drain_background_tasks(adapter: WecomBotAdapter) -> None:
    pending = list(adapter._inflight_emits)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_start_reconnects_after_connection_died() -> None:
    """`start()` 的语义是"确保已连接"：连接被顶掉后必须真的重建，而不是空转。"""
    transports, factory = _recording_factory()
    adapter = _adapter(transport_factory=factory)
    await adapter.start()
    first = transports[-1]
    assert adapter.is_alive() is True

    adapter._handle_event(TAKEOVER_EVENT)
    await _drain_background_tasks(adapter)

    assert adapter.is_alive() is False
    assert first.closed is True  # 旧 socket 必须收干净，否则下一次 subscribe 又顶掉自己

    await adapter.start()

    assert adapter.is_alive() is True
    assert len(transports) == 2
    assert transports[-1] is not first
    assert transports[-1].sent[0]["cmd"] == WS_CMD_SUBSCRIBE
    await adapter.stop()


@pytest.mark.asyncio
async def test_takeover_fails_pending_requests_instead_of_hanging() -> None:
    """被顶号时在飞的出站请求立即失败（-> uncertain），而不是干等 5s 回执超时。"""
    _transports, factory = _recording_factory()
    adapter = _adapter(transport_factory=factory)
    await adapter.start()
    future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
    adapter._pending["aibot_send_msg_pending"] = future

    adapter._handle_event(TAKEOVER_EVENT)

    assert future.done() is True
    assert isinstance(future.exception(), ConnectionError)
    assert adapter._pending == {}
    await _drain_background_tasks(adapter)
    await adapter.stop()


@pytest.mark.asyncio
async def test_connect_never_leaves_two_live_sockets() -> None:
    """单连接不变式：重复建连前必须关掉上一条 socket。"""
    transports, factory = _recording_factory()
    adapter = _adapter(transport_factory=factory)
    await adapter.start()
    first = transports[-1]

    await adapter._connect()

    assert first.closed is True
    assert len(transports) == 2
    await adapter.stop()


@pytest.mark.asyncio
async def test_stale_receive_loop_cannot_mark_new_connection_dead() -> None:
    """重连后，旧接收循环的收尾不得把新连接的 `_connected` 置回 False。"""
    transports, factory = _recording_factory()
    adapter = _adapter(transport_factory=factory)
    await adapter.start()
    stale = transports[-1]

    async def boom() -> dict:
        raise ConnectionError("old socket closed")

    adapter._transport = FakeWecomTransport()  # 假装已经换了新连接
    adapter._connected = True
    stale.recv_json = boom  # type: ignore[method-assign]
    stale.push({"cmd": "ignored"})  # 唤醒旧循环，它下一次 recv 就会炸
    await asyncio.sleep(0.05)

    assert adapter._connected is True
    assert adapter.is_alive() is True
    await adapter.stop()
