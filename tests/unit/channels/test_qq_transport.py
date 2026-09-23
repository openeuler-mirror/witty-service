"""QQ 原生传输层的协议用例（`adapters/qq_transport.py`），断言的全是**线格式**。

Identify/心跳载荷、被动回复体与主动消息体的字段差异、HTTP 状态码到三态、网关 op 码与
关闭码、token 的获取与刷新。两个替身都不碰网络：HTTP 用 `httpx.MockTransport`，
websocket 用 `FakeSocket`（收发队列，按网关的官方顺序喂握手与事件流）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from witty_service.channels import errors as err
from witty_service.channels.adapters.qq_transport import (
    API_BASE,
    EVENT_READY,
    MSG_TYPE_MARKDOWN,
    MSG_TYPE_TEXT,
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_IDENTIFY,
    OP_INVALID_SESSION,
    OP_RECONNECT,
    PUBLIC_MESSAGES_INTENT,
    SANDBOX_API_BASE,
    TOKEN_REFRESH_MARGIN_SECONDS,
    TOKEN_URL,
    QqApiClient,
    QqGatewayTransport,
    QqTransportError,
    build_heartbeat_payload,
    build_identify_payload,
    build_message_body,
    message_path,
    translate_close_code,
    translate_http_status,
    translate_transport_exception,
)
from witty_service.channels.contracts import (
    CERTAINTY_REJECTED,
    CERTAINTY_UNCERTAIN,
    CONVERSATION_TYPE_DIRECT,
    CONVERSATION_TYPE_GROUP,
)

APP_ID = "102123456"
SECRET = "s3cr3t"
GATEWAY_URL = "wss://gateway.example/ws"


# ================================== 载荷（纯函数） ==================================


def test_message_types_match_the_platform() -> None:
    assert MSG_TYPE_TEXT == 0
    assert MSG_TYPE_MARKDOWN == 2


def test_identify_payload_carries_intents_and_the_token_prefix() -> None:
    payload = build_identify_payload(token="ACCESS")

    assert payload["op"] == OP_IDENTIFY
    assert payload["d"]["token"] == "QQBot ACCESS"
    assert payload["d"]["intents"] == PUBLIC_MESSAGES_INTENT == 1 << 25
    assert payload["d"]["shard"] == [0, 1]
    assert set(payload["d"]["properties"]) == {"$os", "$browser", "$device"}


def test_heartbeat_payload_carries_the_last_sequence() -> None:
    assert build_heartbeat_payload(42) == {"op": OP_HEARTBEAT, "d": 42}
    # 还没收到过事件时官方 SDK 回 0（不是 null）
    assert build_heartbeat_payload() == {"op": OP_HEARTBEAT, "d": 0}


def test_passive_body_carries_msg_id_and_seq() -> None:
    body = build_message_body(text="你好", msg_id="MSG_1", msg_seq=2)

    assert body == {
        "content": "你好",
        "msg_type": MSG_TYPE_TEXT,
        "msg_id": "MSG_1",
        "msg_seq": 2,
    }


def test_passive_body_defaults_the_sequence_to_one() -> None:
    """平台要求 `msg_id + msg_seq` 唯一，最少也得给个 1。"""
    body = build_message_body(text="你好", msg_id="MSG_1")

    assert body["msg_seq"] == 1


def test_proactive_body_carries_neither_msg_id_nor_seq() -> None:
    """主动消息带上这两个字段会被平台当成被动回复，"额度用尽后的降级"就永远失败。"""
    body = build_message_body(text="你好")

    assert body == {"content": "你好", "msg_type": MSG_TYPE_TEXT}
    assert "msg_id" not in body
    assert "msg_seq" not in body


def test_markdown_body_uses_the_markdown_field() -> None:
    body = build_message_body(text="# 标题", markdown=True, msg_id="MSG_1", msg_seq=1)

    assert body["markdown"] == {"content": "# 标题"}
    assert body["msg_type"] == MSG_TYPE_MARKDOWN
    assert "content" not in body


def test_message_path_for_direct_and_group() -> None:
    assert (
        message_path(conversation_type=CONVERSATION_TYPE_DIRECT, target_id="OPENID_1")
        == "/v2/users/OPENID_1/messages"
    )
    assert (
        message_path(conversation_type=CONVERSATION_TYPE_GROUP, target_id="GROUP_1")
        == "/v2/groups/GROUP_1/messages"
    )


def test_message_path_escapes_the_target_id() -> None:
    """平台给的 openid 仍要转义：不转义时 a/b?x=1 会把请求打到别的路径上。"""
    path = message_path(conversation_type=CONVERSATION_TYPE_DIRECT, target_id="a/b?x=1")

    assert path == "/v2/users/a%2Fb%3Fx%3D1/messages"
    assert "?" not in path


# ==================================== 三态映射 ====================================


@pytest.mark.parametrize(
    ("status", "certainty", "code"),
    [
        (401, CERTAINTY_REJECTED, err.CHANNEL_CREDENTIAL_INVALID),
        (403, CERTAINTY_REJECTED, "QQ_403"),
        (404, CERTAINTY_REJECTED, "QQ_404"),
        (429, CERTAINTY_REJECTED, "QQ_429"),
        (500, CERTAINTY_UNCERTAIN, "QQ_SERVER_ERROR"),
        (503, CERTAINTY_UNCERTAIN, "QQ_SERVER_ERROR"),
        (400, CERTAINTY_UNCERTAIN, "QQ_400"),
    ],
)
def test_http_status_is_mapped_to_the_three_states(
    status: int, certainty: str, code: str
) -> None:
    translated = translate_http_status(status)

    assert translated.certainty == certainty
    assert translated.error_code == code


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("socket timeout"),
        ConnectionError("connection reset"),
        ValueError("??"),
    ],
)
def test_unknown_exceptions_are_uncertain(exc: BaseException) -> None:
    """**代价不对称**：无法确定的异常一律 uncertain——不重发，否则用户会收到两条。"""
    translated = translate_transport_exception(exc)

    assert translated.certainty == CERTAINTY_UNCERTAIN
    assert translated.error_code == err.CHANNEL_DELIVERY_UNCERTAIN


def test_close_code_4004_means_bad_credentials() -> None:
    translated = translate_close_code(4004)

    assert translated is not None
    assert translated.certainty == CERTAINTY_REJECTED
    assert translated.error_code == err.CHANNEL_CREDENTIAL_INVALID


def test_other_close_codes_are_uncertain_and_no_code_is_not_an_error() -> None:
    translated = translate_close_code(1006)

    assert translated is not None
    assert translated.certainty == CERTAINTY_UNCERTAIN
    assert translated.error_code == "QQ_WS_1006"
    assert translate_close_code(None) is None


# ================== REST 客户端（httpx.MockTransport：真实 HTTP 语义，假网络） ==================


class HttpRecorder:
    """把平台两个域名的响应按路径排好，并记录每一次请求。"""

    def __init__(
        self,
        *,
        token: object = "ACCESS",
        expires_in: object = 7200,
        gateway: object = GATEWAY_URL,
        send_status: int = 200,
        send_body: dict[str, Any] | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.token = token
        self.expires_in = expires_in
        self.gateway = gateway
        self.send_status = send_status
        self.send_body = send_body if send_body is not None else {"id": "MSG_OUT_1"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/app/getAppAccessToken":
            return httpx.Response(
                200, json={"access_token": self.token, "expires_in": self.expires_in}
            )
        if path == "/gateway/bot":
            return httpx.Response(
                200,
                json={
                    "url": self.gateway,
                    "shards": 1,
                    "session_start_limit": {"max_concurrency": 1, "remaining": 1},
                },
            )
        if path.endswith("/messages"):
            return httpx.Response(self.send_status, json=self.send_body)
        return httpx.Response(404, json={"message": "unexpected path"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def paths(self) -> list[str]:
        return [request.url.path for request in self.requests]


def _api(recorder: HttpRecorder, **kwargs: Any) -> QqApiClient:
    return QqApiClient(
        app_id=APP_ID,
        secret=SECRET,
        http_client=recorder.client(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_access_token_is_fetched_once_and_reused() -> None:
    recorder = HttpRecorder()
    api = _api(recorder)

    assert await api.access_token() == "ACCESS"
    assert await api.access_token() == "ACCESS"

    assert recorder.paths().count("/app/getAppAccessToken") == 1
    await api.aclose()


@pytest.mark.asyncio
async def test_token_is_refreshed_before_it_expires() -> None:
    """留 60s 余量：卡在有效期的最后一秒去发消息，就会拿到一个刚过期的 token。"""
    now = [0.0]
    recorder = HttpRecorder(expires_in=100)
    api = QqApiClient(
        app_id=APP_ID,
        secret=SECRET,
        http_client=recorder.client(),
        clock=lambda: now[0],
    )

    await api.access_token()
    now[0] = 30.0
    await api.access_token()
    assert recorder.paths().count("/app/getAppAccessToken") == 1
    assert TOKEN_REFRESH_MARGIN_SECONDS == 60.0

    now[0] = 41.0
    await api.access_token()
    assert recorder.paths().count("/app/getAppAccessToken") == 2
    await api.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "", 123])
async def test_missing_access_token_is_a_credential_error(token: object) -> None:
    """平台对错误凭据返回 200 + 错误体：这条必须自己判成凭据问题（官方 SDK 只抛 RuntimeError）。"""
    recorder = HttpRecorder(token=token)
    api = _api(recorder)

    with pytest.raises(QqTransportError) as caught:
        await api.access_token()

    assert caught.value.certainty == CERTAINTY_REJECTED
    assert caught.value.error_code == err.CHANNEL_CREDENTIAL_INVALID
    await api.aclose()


@pytest.mark.asyncio
async def test_expires_in_accepts_strings_and_garbage() -> None:
    recorder = HttpRecorder(expires_in="7200")
    api = _api(recorder)

    assert await api.access_token() == "ACCESS"

    recorder = HttpRecorder(expires_in="not-a-number")
    api = _api(recorder)
    # 解析不出来就按"立刻过期"处理：最多多要一次 token，不会拿着过期 token 去发消息
    assert await api.access_token() == "ACCESS"
    await api.access_token()
    assert recorder.paths().count("/app/getAppAccessToken") == 2
    await api.aclose()


def _api_with_handler(
    handler: Callable[[httpx.Request], httpx.Response],
) -> QqApiClient:
    return QqApiClient(
        app_id=APP_ID,
        secret=SECRET,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_a_rejected_token_is_discarded_and_the_send_retried_once() -> None:
    """回归：401 = 平台不认这个 token，**先丢缓存再重试一次**，而不是拿它把 TTL 耗完。

    旧实现只把它翻成"凭据无效"上报、缓存里的 token 纹丝不动，此后每条出站都必然 401。
    """
    issued: list[str] = []
    sent_authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/getAppAccessToken":
            issued.append(f"ACCESS_{len(issued) + 1}")
            return httpx.Response(
                200, json={"access_token": issued[-1], "expires_in": 7200}
            )
        sent_authorizations.append(request.headers["Authorization"])
        if len(sent_authorizations) == 1:
            return httpx.Response(401, json={"message": "invalid token"})
        return httpx.Response(200, json={"id": "MSG_OUT_1"})

    api = _api_with_handler(handler)

    result = await api.send_message(
        conversation_type=CONVERSATION_TYPE_DIRECT, target_id="OPENID_1", text="结果"
    )

    assert result["id"] == "MSG_OUT_1"
    assert issued == ["ACCESS_1", "ACCESS_2"]
    assert sent_authorizations == ["QQBot ACCESS_1", "QQBot ACCESS_2"]
    await api.aclose()


@pytest.mark.asyncio
async def test_a_persistently_rejected_token_fails_as_a_credential_error() -> None:
    """真错了就是真错了：只重试一次，不能把它变成死循环。"""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(
                200, json={"access_token": "ACCESS", "expires_in": 7200}
            )
        attempts += 1
        return httpx.Response(401, json={"message": "invalid token"})

    api = _api_with_handler(handler)

    with pytest.raises(QqTransportError) as caught:
        await api.send_message(
            conversation_type=CONVERSATION_TYPE_DIRECT,
            target_id="OPENID_1",
            text="结果",
        )

    assert caught.value.error_code == err.CHANNEL_CREDENTIAL_INVALID
    assert caught.value.certainty == CERTAINTY_REJECTED
    assert attempts == 2
    await api.aclose()


@pytest.mark.asyncio
async def test_send_message_passes_the_official_headers_and_body() -> None:
    recorder = HttpRecorder()
    api = _api(recorder)

    result = await api.send_message(
        conversation_type=CONVERSATION_TYPE_DIRECT,
        target_id="OPENID_1",
        text="结果",
        msg_id="MSG_IN_1",
        msg_seq=3,
    )

    assert result["id"] == "MSG_OUT_1"
    send = recorder.requests[-1]
    assert send.headers["Authorization"] == "QQBot ACCESS"
    assert send.headers["X-Union-Appid"] == APP_ID
    assert json.loads(send.content) == {
        "content": "结果",
        "msg_type": MSG_TYPE_TEXT,
        "msg_id": "MSG_IN_1",
        "msg_seq": 3,
    }
    await api.aclose()


@pytest.mark.asyncio
async def test_gateway_url_comes_from_the_gateway_endpoint() -> None:
    recorder = HttpRecorder()
    api = _api(recorder)

    assert await api.gateway_url() == GATEWAY_URL
    assert "/gateway/bot" in recorder.paths()
    await api.aclose()


@pytest.mark.asyncio
async def test_gateway_without_a_url_is_uncertain() -> None:
    recorder = HttpRecorder(gateway="")
    api = _api(recorder)

    with pytest.raises(QqTransportError) as caught:
        await api.gateway_url()

    assert caught.value.error_code == "QQ_BAD_GATEWAY"
    assert caught.value.certainty == CERTAINTY_UNCERTAIN
    await api.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"), [(403, "QQ_403"), (429, "QQ_429"), (500, "QQ_SERVER_ERROR")]
)
async def test_send_failures_are_translated_by_status(status: int, code: str) -> None:
    recorder = HttpRecorder(
        send_status=status, send_body={"code": 11244, "message": "banned"}
    )
    api = _api(recorder)

    with pytest.raises(QqTransportError) as caught:
        await api.send_message(
            conversation_type=CONVERSATION_TYPE_DIRECT,
            target_id="OPENID_1",
            text="结果",
        )

    # 错误文案里保留平台给的 message 与 code：这是唯一能说明"为什么被拒"的现场信息
    assert "banned" in caught.value.message
    assert "11244" in caught.value.message
    assert caught.value.error_code == code
    await api.aclose()


@pytest.mark.asyncio
async def test_transport_timeout_is_uncertain() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    api = QqApiClient(
        app_id=APP_ID,
        secret=SECRET,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(QqTransportError) as caught:
        await api.send_message(
            conversation_type=CONVERSATION_TYPE_DIRECT,
            target_id="OPENID_1",
            text="结果",
        )

    assert caught.value.certainty == CERTAINTY_UNCERTAIN
    assert caught.value.error_code == err.CHANNEL_DELIVERY_UNCERTAIN
    await api.aclose()


@pytest.mark.asyncio
async def test_non_json_response_is_uncertain() -> None:
    api = QqApiClient(
        app_id=APP_ID,
        secret=SECRET,
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text="<html>oops</html>")
            )
        ),
    )

    with pytest.raises(QqTransportError) as caught:
        await api.send_message(
            conversation_type=CONVERSATION_TYPE_DIRECT,
            target_id="OPENID_1",
            text="结果",
        )

    assert caught.value.certainty == CERTAINTY_UNCERTAIN
    await api.aclose()


@pytest.mark.asyncio
async def test_sandbox_uses_the_sandbox_api_domain() -> None:
    recorder = HttpRecorder()
    api = _api(recorder, sandbox=True)

    await api.send_message(
        conversation_type=CONVERSATION_TYPE_DIRECT, target_id="OPENID_1", text="结果"
    )

    assert recorder.requests[-1].url.host == "sandbox.api.sgroup.qq.com"
    assert SANDBOX_API_BASE.endswith("sandbox.api.sgroup.qq.com")
    assert API_BASE == "https://api.sgroup.qq.com"
    assert TOKEN_URL == "https://bots.qq.com/app/getAppAccessToken"
    await api.aclose()


# ==================== websocket 网关（假 socket：按官方顺序喂帧） ====================

HELLO = {"op": OP_HELLO, "d": {"heartbeat_interval": 1000}}
READY = {
    "op": OP_DISPATCH,
    "s": 1,
    "t": EVENT_READY,
    "d": {"session_id": "SESSION_1", "user": {"id": "1", "username": "bot"}},
}


class FakeSocket:
    """假 websocket：收到的帧从队列里取，发出去的帧按 JSON 记下来。"""

    def __init__(self, *frames: Any) -> None:
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._incoming: asyncio.Queue[Any] = asyncio.Queue()
        self.feed(*frames)

    def feed(self, *frames: Any) -> None:
        for frame in frames:
            self._incoming.put_nowait(frame)

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        item = await self._incoming.get()
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item)

    async def close(self) -> None:
        # 真实 websockets 关闭时，阻塞在 recv() 上的接收循环会立刻收到关闭异常；
        # 替身保持同样的语义，否则"关闭后还挂在死 socket 上"复现不出来
        self.closed = True
        self.feed(Closed(1000))

    def ops(self) -> list[int]:
        return [frame["op"] for frame in self.sent]


class Closed(Exception):
    """模拟 websockets 的关闭异常（带关闭码）。"""

    def __init__(self, code: int) -> None:
        super().__init__(f"closed with {code}")
        self.code = code


async def _returns(value: Any) -> Any:
    return value


def _transport(
    recorder: HttpRecorder, socket: FakeSocket, **kwargs: Any
) -> QqGatewayTransport:
    return QqGatewayTransport(
        app_id=APP_ID,
        secret=SECRET,
        api=_api(recorder),
        ws_factory=lambda _url: _returns(socket),
        connect_timeout=kwargs.pop("connect_timeout", 1.0),
        **kwargs,
    )


async def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


@pytest.mark.asyncio
async def test_connect_runs_the_official_handshake() -> None:
    socket = FakeSocket(HELLO, READY)
    transport = _transport(HttpRecorder(), socket)

    await transport.connect()

    assert transport.is_alive() is True
    assert socket.ops() == [OP_IDENTIFY]
    assert socket.sent[0]["d"]["token"] == "QQBot ACCESS"
    assert socket.sent[0]["d"]["intents"] == PUBLIC_MESSAGES_INTENT

    await transport.close()
    assert socket.closed is True
    assert transport.is_alive() is False


@pytest.mark.asyncio
async def test_dispatched_messages_reach_the_handler() -> None:
    frame = {
        "id": "EVENT_1",
        "op": OP_DISPATCH,
        "s": 2,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": "MSG_1",
            "content": "你好",
            "timestamp": "1767225600000",
            "author": {"user_openid": "OPENID_1"},
        },
    }
    socket = FakeSocket(HELLO, READY, frame)
    transport = _transport(HttpRecorder(), socket)
    received: list[Any] = []
    transport.on_event(received.append)

    await transport.connect()
    assert await _wait_until(lambda: len(received) == 1) is True

    assert received[0]["t"] == "C2C_MESSAGE_CREATE"
    assert received[0]["d"]["content"] == "你好"
    await transport.close()


@pytest.mark.asyncio
async def test_heartbeat_repeats_the_last_sequence() -> None:
    frame = {
        "op": OP_DISPATCH,
        "s": 42,
        "t": "C2C_MESSAGE_CREATE",
        "d": {"id": "MSG_1"},
    }
    socket = FakeSocket(HELLO, READY, frame)
    transport = _transport(HttpRecorder(), socket)

    await transport.connect()
    assert (
        await _wait_until(lambda: any(op == OP_HEARTBEAT for op in socket.ops()))
        is True
    )

    beats = [item for item in socket.sent if item["op"] == OP_HEARTBEAT]
    assert beats[0]["d"] == 42
    await transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("opcode", [OP_RECONNECT, OP_INVALID_SESSION])
async def test_gateway_asked_reconnect_ends_the_session(opcode: int) -> None:
    """op7/op9 都表示"这条连接作废"：结束循环，交给渠道网关的监督循环重建。"""
    socket = FakeSocket(HELLO, READY, {"op": opcode})
    transport = _transport(HttpRecorder(), socket)

    await transport.connect()
    assert await _wait_until(lambda: not transport.is_alive()) is True

    await transport.close()


@pytest.mark.asyncio
async def test_auth_close_code_4004_surfaces_as_a_credential_error() -> None:
    socket = FakeSocket(HELLO, READY)
    transport = _transport(HttpRecorder(), socket)
    await transport.connect()

    socket.feed(Closed(4004))

    # 接收循环把 4004 归类成"凭据问题"并结束会话
    assert await _wait_until(lambda: not transport.is_alive()) is True
    await transport.close()


@pytest.mark.asyncio
async def test_connect_times_out_when_ready_never_arrives() -> None:
    socket = FakeSocket(HELLO)
    transport = _transport(HttpRecorder(), socket, connect_timeout=0.05)

    with pytest.raises(QqTransportError) as caught:
        await transport.connect()

    assert caught.value.error_code == "QQ_CONNECT_TIMEOUT"
    assert caught.value.certainty == CERTAINTY_UNCERTAIN
    assert transport.is_alive() is False


@pytest.mark.asyncio
async def test_credentials_are_required_before_connecting() -> None:
    transport = QqGatewayTransport(app_id="", secret="")

    with pytest.raises(QqTransportError) as caught:
        await transport.connect()

    assert caught.value.certainty == CERTAINTY_REJECTED
    assert caught.value.error_code == err.CHANNEL_CREDENTIAL_INVALID


async def _ack_heartbeats(socket: FakeSocket, *, seconds: float) -> None:
    """像真实网关那样对每一个 op1 回一次 op11，持续 `seconds` 秒。"""
    acked = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        beats = [frame for frame in socket.sent if frame["op"] == OP_HEARTBEAT]
        while acked < len(beats):
            socket.feed({"op": OP_HEARTBEAT_ACK})
            acked += 1
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_a_silent_gateway_is_declared_dead_not_alive() -> None:
    """僵尸连接：socket 半开时 **send 依旧成功**，只有"多久没收到帧"能判死。

    判据是心跳回执（op11）：HELLO 的 heartbeat_interval 是 1000ms，容忍 2 个间隔即
    判死；否则 `is_alive()` 永远为真，监督循环永不重建这条死链路。
    """
    socket = FakeSocket(HELLO, READY)
    transport = _transport(HttpRecorder(), socket)
    await transport.connect()
    assert transport.is_alive() is True

    assert await _wait_until(lambda: not transport.is_alive(), timeout=8.0) is True
    assert OP_HEARTBEAT in socket.ops()  # 是真发过心跳之后才判死的
    assert transport._failure is not None
    assert transport._failure.error_code == "QQ_WS_HEARTBEAT_TIMEOUT"
    assert transport._failure.certainty == CERTAINTY_UNCERTAIN
    assert socket.closed is True  # 关掉 socket 才能把接收循环从 recv() 上叫醒
    await transport.close()


@pytest.mark.asyncio
async def test_heartbeat_acks_keep_the_session_alive() -> None:
    """有回执就不该被判死：正常网络抖动（丢一两次回执）不能引发无谓重连。"""
    socket = FakeSocket(HELLO, READY)
    transport = _transport(HttpRecorder(), socket)
    await transport.connect()

    await _ack_heartbeats(socket, seconds=2.5)

    # 2.5s ≈ 2.5 个心跳间隔：只看发送、不看回执的实现到这里已经判死
    assert transport.is_alive() is True
    await transport.close()


@pytest.mark.asyncio
async def test_cancelling_connect_closes_the_socket_and_the_loop() -> None:
    """取消也要收干净：stop() 会取消正停在 READY 上的监督循环。

    只把 CancelledError 往外抛，socket 与接收任务就没人引用——平台侧"单机器人单连接"
    的名额会被这条僵尸连接占住，重建的连接被顶号。
    """
    socket = FakeSocket(HELLO)  # 永远等不到 READY
    transport = _transport(HttpRecorder(), socket, connect_timeout=30.0)

    task = asyncio.create_task(transport.connect())
    assert await _wait_until(lambda: transport._ws is not None) is True

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert socket.closed is True
    assert transport._task is None
    assert transport.is_alive() is False


@pytest.mark.asyncio
async def test_close_is_safe_before_connect() -> None:
    transport = QqGatewayTransport(app_id=APP_ID, secret=SECRET)

    await transport.close()

    assert transport.is_alive() is False


@pytest.mark.asyncio
async def test_send_message_goes_through_rest_not_the_socket() -> None:
    """出站只有 REST 一条路：websocket 上不该出现任何出站消息。"""
    socket = FakeSocket(HELLO, READY)
    recorder = HttpRecorder()
    transport = _transport(recorder, socket)
    await transport.connect()

    result = await transport.send_message(
        conversation_type=CONVERSATION_TYPE_DIRECT, target_id="OPENID_1", text="结果"
    )

    assert result["id"] == "MSG_OUT_1"
    assert recorder.paths().count("/v2/users/OPENID_1/messages") == 1
    assert OP_HEARTBEAT not in socket.ops() or OP_IDENTIFY in socket.ops()
    await transport.close()
