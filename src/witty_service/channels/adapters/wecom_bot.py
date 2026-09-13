"""企业微信机器人适配器（标杆渠道）：**官方协议**的原生 WebSocket 实现。

## 协议来源（不是猜的）

1. 官方 Node SDK `@wecom/aibot-node-sdk@1.0.7`（本机 `~/.dsh/profiles/web/node_modules/`）：
   - WS 地址：`wss://openws.work.weixin.qq.com`（SDK 常量 `DEFAULT_WS_URL`）；
   - 帧取值域：`aibot_subscribe`（认证订阅）、`ping`（心跳）、`aibot_respond_msg`（被动回复）、
     `aibot_send_msg`（主动发送）、`aibot_msg_callback`（消息推送）、`aibot_event_callback`（事件推送）；
   - **回执帧没有 `cmd`**：靠 `headers.req_id` 前缀关联，用 `errcode` / `errmsg` 判定；
     回执超时 5s；心跳 30s，连续 2 次未回视为断线；服务端推送
     `disconnected_event`（已有新连接）时**不再自动重连**。
2. 参考实现 dsh-im（`refs/dsh-im/src/channels/wecom/`）：入站字段
   `body.msgid` / `body.from.userid` / `body.chattype`(single|group) / `body.chatid` /
   `body.msgtype` / `body.text.content` / `body.voice.content`（平台语音转写）/
   `body.mixed.msg_item[]`；主动发送时单聊的 `chatid` 就是 userid。
3. 实测：`GET https://work.weixin.qq.com/ai/qc/generate?source=..&plat=3` 返回
   `{"data":{"scode","auth_url"}}`；而早期猜测的 `qyapi.../cgi-bin/channel/provision/qr` 返回 404。

企微没有官方 Python SDK，因此这里按上述协议原生实现；传输层仍隔离在 `WecomTransport`
后面，测试用假传输驱动，不需要真实平台。

## 出站为什么用 `aibot_send_msg` 而不是 `aibot_respond_msg`

被动回复必须回带**当次回调的 `req_id`**，而本项目的出站是"占位消息 → 终稿"两条独立消息，
终稿可能在几分钟后才发（长回合延迟补发），那时回调早已过期。因此所有出站都走主动发送，
`chatid` 取路由里的平台用户标识（单聊即 userid）。

## 主动发送的合法消息类型只有 markdown / template_card / 媒体（**没有 text**）

官方 SDK 的类型定义给出了穷举：

```ts
type SendMsgBody = SendMarkdownMsgBody | SendTemplateCardMsgBody | SendMediaMsgBody;  // dist/index.d.ts:600
interface SendMarkdownMsgBody { msgtype: 'markdown'; markdown: { content: string } }  // :583
```

`msgtype: "text"` 只在两处合法：**入站回调**（用户发来的文本）与**欢迎语被动回复**
（`aibot_respond_welcome_msg`，`WelcomeTextReplyBody`）。早期版本把入站字段名直接搬来做出站，
平台对 `aibot_send_msg` + `msgtype:"text"` 回 **errcode 40008（不合法的消息类型）**，
表现为"手机上机器人完全不回话"（真机实测：2026-09-13 13:20:41 一条消息发送
`certainty=uncertain code=WECOM_40008`）。参考实现同样只用 markdown：
`refs/dsh-im/src/channels/wecom/wecom-bridge.mjs:1189 sendMessage(chatId, {msgtype:'markdown', markdown:{content}})`。

通用被动回复只支持 `stream` / `template_card`：`ReplyStream` 的 `stream.content` 上限
20480 字节，用 `{id, finish:true, content}` 一次性结束。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Protocol
from urllib.parse import urlencode
from uuid import uuid4

import websockets.exceptions
from websockets.asyncio.client import connect as ws_connect

from witty_service.channels import errors as err
from witty_service.channels.adapters.base import BaseChannelAdapter, CredentialField
from witty_service.channels.contracts import (
    CONVERSATION_TYPE_DIRECT,
    CONVERSATION_TYPE_GROUP,
    UNSUPPORTED_FILE,
    UNSUPPORTED_IMAGE,
    UNSUPPORTED_UNKNOWN,
    UNSUPPORTED_VOICE,
    ChannelCapabilities,
    DeliveryResult,
    InboundMessage,
    Route,
    register_adapter,
)
from witty_service.channels.provisioning.drivers import (
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_WAITING,
    ProvisioningOutcome,
    ProvisioningSession,
    register_driver,
)

logger = logging.getLogger(__name__)

ADAPTER_VERSION = "0.2.1"

#: 单条文本上限：平台未公开该限制，沿用保守值（真机确认后可上调，未决项 U2）
MAX_TEXT_LENGTH = 2000

# ==============================================================================
# 协议常量（取自官方 SDK）
# ==============================================================================

WS_CMD_SUBSCRIBE = "aibot_subscribe"
WS_CMD_HEARTBEAT = "ping"
WS_CMD_SEND = "aibot_send_msg"
WS_CMD_RESPOND = "aibot_respond_msg"
WS_CMD_MSG_CALLBACK = "aibot_msg_callback"
WS_CMD_EVENT_CALLBACK = "aibot_event_callback"

CONVERSATION_TYPE_SINGLE = "single"
CONVERSATION_TYPE_GROUP_WECOM = "group"

#: 出站消息类型：主动发送只接受 markdown / template_card / 媒体（见模块 docstring）
MSGTYPE_MARKDOWN = "markdown"
#: 通用被动文本回复用 stream 承载（无 text）
MSGTYPE_STREAM = "stream"
#: 入站文本的消息类型（用户发来的消息才是 text）
MSGTYPE_INBOUND_TEXT = "text"

#: 长连接网关地址（官方 SDK 内置默认值；可经实例 config.ws_url 覆盖）
DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"

#: 单条出站等待平台回执的超时（秒），与官方 SDK 的 replyAckTimeout 一致
DEFAULT_ACK_TIMEOUT_SECONDS = 5.0
#: 认证订阅等待回执的超时（秒），与官方 SDK 的 20s 一致
DEFAULT_AUTH_TIMEOUT_SECONDS = 20.0
#: 心跳间隔与容忍的连续丢失次数（与官方 SDK 一致）
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30.0
MAX_MISSED_HEARTBEATS = 2

#: 入站字段（参考实现 dsh-im 的 wecom-bridge 只读这些）
_CALLBACK_EVENT_KEYS = ("msgid", "event_id", "id")
_CALLBACK_USER_KEYS = ("userid", "user_id", "open_userid")
_CALLBACK_TYPE_KEYS = ("msgtype", "msg_type")
_CALLBACK_CHAT_KEYS = ("chattype", "chat_type")

#: 平台确定性拒绝的 errcode -> 复用"凭据失效"文案
CREDENTIAL_ERROR_CODES = frozenset({"-1", "40001", "40014", "41001", "42001"})

_UNSUPPORTED_MSG_TYPES = {
    "image": UNSUPPORTED_IMAGE,
    "file": UNSUPPORTED_FILE,
    "video": UNSUPPORTED_FILE,
    "attachment": UNSUPPORTED_FILE,
}

# ==============================================================================
# 扫码接入（企业微信官方 device flow）
# ==============================================================================

#: 二维码服务地址与两个端点（与官方实现一致：不在 qyapi 上）
QR_SERVICE_BASE = "https://work.weixin.qq.com"
QR_GENERATE_PATH = "/ai/qc/generate"
QR_POLL_PATH = "/ai/qc/query_result"
#: 二维码有效期与轮询间隔（官方实现常量：5 分钟 / 3 秒）
QR_TTL_SECONDS = 5 * 60
QR_POLL_INTERVAL_MS = 3000
#: 官方实现校验 auth_url 必须落在该主机上
QR_ALLOWED_HOST = "work.weixin.qq.com"
#: 生成二维码时上报的调用方标识
DEFAULT_SOURCE = "witty-service"

_QR_SUCCESS_STATES = frozenset({"success", "succeeded", "ok"})
_QR_EXPIRED_STATES = frozenset({"expired", "timeout"})
_QR_FAILED_STATES = frozenset({"fail", "failed", "error", "rejected"})


def default_platform_code() -> int:
    """`plat` 取值：win32 -> 2，linux -> 3，其余 -> 1（与官方实现一致）。"""
    if sys.platform.startswith("win"):
        return 2
    if sys.platform.startswith("linux"):
        return 3
    return 1


class WecomProtocolError(Exception):
    """平台返回的**确定性**业务错误（鉴权失败、权限、目标不存在等）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def is_credential_error(self) -> bool:
        return self.code in CREDENTIAL_ERROR_CODES


class WecomTransport(Protocol):
    """传输层接口：把"原生 WebSocket"隔离在一个可替换的薄层里。"""

    async def connect(self) -> None: ...

    async def send_json(self, payload: dict[str, Any]) -> None: ...

    async def recv_json(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class WebSocketWecomTransport:
    """默认传输层：原生 WebSocket（`websockets` 已在主依赖中）。"""

    def __init__(self, url: str, *, open_timeout: float = 10.0) -> None:
        self._url = url
        self._open_timeout = open_timeout
        self._ws: Any = None

    async def connect(self) -> None:
        self._ws = await ws_connect(
            self._url, open_timeout=self._open_timeout, ping_interval=None
        )

    async def send_json(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise ConnectionError("wecom websocket is not connected")
        await self._ws.send(json.dumps(payload, ensure_ascii=False))

    async def recv_json(self) -> dict[str, Any]:
        if self._ws is None:
            raise ConnectionError("wecom websocket is not connected")
        raw = await self._ws.recv()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise WecomProtocolError("BAD_FRAME", "wecom frame is not an object")
        return data

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()


TransportFactory = Callable[[str], WecomTransport]

HttpGet = Callable[[str], Awaitable[Mapping[str, Any]]]


# ==============================================================================
# 帧构造与归一化（纯函数，便于表驱动测试）
# ==============================================================================


def build_req_id(prefix: str) -> str:
    """`{prefix}_{timestamp}_{random}`：平台按 **req_id 前缀**区分回执类型。"""
    return f"{prefix}_{int(datetime.now(UTC).timestamp() * 1000)}_{uuid4().hex[:8]}"


def build_subscribe_frame(
    bot_id: str, secret: str, *, req_id: str
) -> dict[str, Any]:
    """认证订阅帧：`{cmd:"aibot_subscribe", headers:{req_id}, body:{bot_id, secret}}`。"""
    return {
        "cmd": WS_CMD_SUBSCRIBE,
        "headers": {"req_id": req_id},
        "body": {"bot_id": bot_id, "secret": secret},
    }


def build_heartbeat_frame(*, req_id: str) -> dict[str, Any]:
    return {"cmd": WS_CMD_HEARTBEAT, "headers": {"req_id": req_id}, "body": {}}


def build_send_frame(*, req_id: str, chat_id: str, text: str) -> dict[str, Any]:
    """主动发送帧（单聊的 `chatid` 就是用户 userid）。

    **消息类型必须是 `markdown`**：`SendMsgBody` 的合法取值只有 markdown / template_card /
    媒体三种，`msgtype:"text"` 会被平台判为 40008（不合法的消息类型），用户侧表现为
    "机器人不回话"。文本内容原样作为 markdown 承载，纯文本同样是合法 markdown。
    """
    return {
        "cmd": WS_CMD_SEND,
        "headers": {"req_id": req_id},
        "body": {
            "chatid": chat_id,
            "msgtype": MSGTYPE_MARKDOWN,
            "markdown": {"content": text},
        },
    }


def build_respond_frame(
    *, req_id: str, text: str, stream_id: str | None = None
) -> dict[str, Any]:
    """被动回复帧：必须回带**当次回调的 req_id**（本项目出站不用它，仅保留协议完整性）。

    通用被动回复同样没有 `text`：文本要用 `stream` 承载，`finish=true` 表示一次性结束。
    """
    return {
        "cmd": WS_CMD_RESPOND,
        "headers": {"req_id": req_id},
        "body": {
            "msgtype": MSGTYPE_STREAM,
            "stream": {
                "id": stream_id or req_id,
                "finish": True,
                "content": text,
            },
        },
    }


def is_ack_frame(frame: Mapping[str, Any]) -> bool:
    """回执帧 = **没有 cmd**、有 headers.req_id（官方 SDK 的判定方式）。"""
    return not frame.get("cmd") and bool(_req_id_of(frame))


def _req_id_of(frame: Mapping[str, Any]) -> str:
    headers = frame.get("headers")
    if not isinstance(headers, Mapping):
        return ""
    value = headers.get("req_id")
    return value if isinstance(value, str) else ""


def normalize_callback(
    frame: Mapping[str, Any], *, instance_id: str
) -> InboundMessage | None:
    """把一帧消息推送归一化为 `InboundMessage`；非消息帧返回 None。

    只处理 `aibot_msg_callback`：`aibot_event_callback`（进会话事件、卡片事件、
    服务端断开通知）不是用户消息，由适配器自己消费。

    不支持的内容类型**也要上报**（`text=None` + `unsupported_kind`），由上层统一
    回复降级文案——这样"需要明确告知用户"的逻辑只有一处。
    """
    if frame.get("cmd") != WS_CMD_MSG_CALLBACK:
        return None
    body = frame.get("body")
    if not isinstance(body, Mapping):
        return None

    event_id = _first_str(body, _CALLBACK_EVENT_KEYS) or _req_id_of(frame)
    if not event_id:
        # 没有事件标识就无法去重：用随机标识兜底，绝不把两条消息合成一条
        event_id = f"fallback-{uuid4().hex}"

    sender = _first_str(body.get("from"), _CALLBACK_USER_KEYS)
    chat_type = (_first_str(body, _CALLBACK_CHAT_KEYS) or "").lower()
    conversation_type = (
        CONVERSATION_TYPE_GROUP
        if chat_type == CONVERSATION_TYPE_GROUP_WECOM
        else CONVERSATION_TYPE_DIRECT
    )

    msg_type = (_first_str(body, _CALLBACK_TYPE_KEYS) or "").lower()
    text: str | None = None
    unsupported_kind: str | None = None
    if msg_type == MSGTYPE_INBOUND_TEXT:
        text = _nested_text(body, "text") or ""
    elif msg_type == "voice":
        # 平台自带语音转写：有转写文本就当文本处理，没有才降级
        transcribed = _nested_text(body, "voice")
        if transcribed:
            text = transcribed
        else:
            unsupported_kind = UNSUPPORTED_VOICE
    elif msg_type == "mixed":
        joined = _mixed_text(body)
        if joined:
            text = joined
        else:
            # 纯图片/文件的图文混排：按"不支持的内容"降级，而不是当成空文本提交
            unsupported_kind = UNSUPPORTED_IMAGE
    else:
        unsupported_kind = _UNSUPPORTED_MSG_TYPES.get(msg_type, UNSUPPORTED_UNKNOWN)

    if conversation_type == CONVERSATION_TYPE_GROUP and text:
        # 群消息里那条 @机器人 是路由信息，不属于用户的问题
        text = _strip_leading_mention(text)

    return InboundMessage(
        platform_event_id=str(event_id),
        route=Route(
            instance_id=instance_id,
            conversation_type=conversation_type,
            platform_user_id=str(sender or ""),
        ),
        text=text,
        unsupported_kind=unsupported_kind,
        received_at=_received_at(body),
    )


def _nested_text(body: Mapping[str, Any], key: str) -> str | None:
    nested = body.get(key)
    if isinstance(nested, Mapping):
        value = nested.get("content")
        if isinstance(value, str) and value.strip():
            return value.strip()
    if isinstance(nested, str) and nested.strip():
        return nested.strip()
    return None


def _mixed_text(body: Mapping[str, Any]) -> str:
    mixed = body.get("mixed")
    if not isinstance(mixed, Mapping):
        return ""
    items = mixed.get("msg_item")
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if isinstance(item, Mapping) and str(item.get("msgtype")) == MSGTYPE_INBOUND_TEXT:
            value = _nested_text(item, "text")
            if value:
                parts.append(value)
    return "\n".join(parts).strip()


def _strip_leading_mention(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith("@"):
        return text.strip()
    _, _, remainder = stripped.partition(" ")
    return (remainder or "").strip() or text.strip()


def _received_at(body: Mapping[str, Any]) -> datetime:
    raw = body.get("create_time")
    if isinstance(raw, (int, float)) and raw > 0:
        seconds = float(raw)
        if seconds > 1e11:  # 毫秒级时间戳
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    return datetime.now(UTC)


def _first_str(source: object, keys: tuple[str, ...]) -> str | None:
    if not isinstance(source, Mapping):
        return None
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)):
            return str(value)
    return None


class WecomBotAdapter(BaseChannelAdapter):
    """企业微信机器人适配器（能力：补发终稿；单条 2000 字符）。"""

    channel = "wecom_bot"
    adapter_version = ADAPTER_VERSION

    display_name = "企业微信机器人"
    #: `bot_id` 与网关地址是非密标识（`bot_id` 额外生成掩码）
    config_fields = ("bot_id", "ws_url")
    mask_fields = ("bot_id",)
    required_credentials = ("bot_id", "secret")
    credential_fields = (
        CredentialField(name="bot_id", label="机器人 ID", secret=False),
        CredentialField(name="secret", label="机器人 Secret"),
        CredentialField(
            name="ws_url", label="长连接地址（留空用默认）", secret=False, required=False
        ),
    )

    def __init__(
        self,
        *,
        instance_id: str = "",
        config: Mapping[str, Any] | None = None,
        credentials: Mapping[str, str] | None = None,
        transport_factory: TransportFactory | None = None,
        ack_timeout_seconds: float = DEFAULT_ACK_TIMEOUT_SECONDS,
        auth_timeout_seconds: float = DEFAULT_AUTH_TIMEOUT_SECONDS,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        max_text_length: int | None = None,
    ) -> None:
        super().__init__(instance_id=instance_id, config=config, credentials=credentials)
        self._transport_factory: TransportFactory = transport_factory or (
            lambda url: WebSocketWecomTransport(url)
        )
        self._ack_timeout = ack_timeout_seconds
        self._auth_timeout = auth_timeout_seconds
        self._heartbeat_interval = heartbeat_interval_seconds
        self._max_text_length = max_text_length or _config_int(
            self._config.get("max_text_length")
        ) or MAX_TEXT_LENGTH
        self._transport: WecomTransport | None = None
        self._receiver: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._inflight_emits: set[asyncio.Task[None]] = set()
        self._connected = False
        self._missed_heartbeats = 0
        self._server_disconnected = False

    # ==========================================================================
    # 生命周期
    # ==========================================================================

    async def _connect(self) -> None:
        # 单连接不变式：同一个适配器绝不留下第二条 socket。平台侧"新连接建立"会踢掉
        # 旧连接（disconnected_event），残留 socket 会让我们自己跟自己抢——表现为
        # 每 15s 被踢一次、消息随机落在即将被丢弃的旧 socket 上。
        if self._transport is not None:
            await self._disconnect()
        url = str(self._config.get("ws_url") or DEFAULT_WS_URL)
        transport = self._transport_factory(url)
        await transport.connect()
        self._transport = transport
        self._server_disconnected = False
        # 接收循环必须先跑起来：认证回执也是从同一条连接读回来的
        self._receiver = asyncio.create_task(self._receive_loop())
        bot_id = str(self._config.get("bot_id") or "")
        secret = str(self._credentials.get("secret") or "")
        try:
            await self._subscribe(bot_id, secret)
        except Exception:
            receiver, self._receiver = self._receiver, None
            if receiver is not None:
                receiver.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await receiver
            self._transport = None
            await transport.close()
            raise
        self._connected = True
        self._missed_heartbeats = 0
        self._heartbeat = asyncio.create_task(self._heartbeat_loop())

    async def _teardown_transport(self) -> None:
        """关闭当前 socket（幂等）；下一次 `_connect` 会在干净状态下重建。"""
        transport, self._transport = self._transport, None
        if transport is None:
            return
        with contextlib.suppress(Exception):
            await transport.close()

    async def _disconnect(self) -> None:
        self._connected = False
        heartbeat, self._heartbeat = self._heartbeat, None
        if heartbeat is not None and not heartbeat.done():
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        receiver, self._receiver = self._receiver, None
        if receiver is not None and not receiver.done():
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver
        for task in list(self._inflight_emits):
            task.cancel()
        self._inflight_emits.clear()
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.close()

    def is_alive(self) -> bool:
        """长连接健康度：接收循环退出或心跳丢失都会把 `_connected` 置 False。"""
        return self._started and self._connected

    async def _probe_capabilities(self) -> ChannelCapabilities:
        """企微没有独立的"编辑消息"接口：呈现方式固定为"补发新消息"。"""
        return ChannelCapabilities(
            can_edit_message=False,
            max_text_length=self._max_text_length,
            max_reply_segments=None,
        )

    # ==========================================================================
    # 连接内部
    # ==========================================================================

    async def _subscribe(self, bot_id: str, secret: str) -> dict[str, Any]:
        return await self._request(
            WS_CMD_SUBSCRIBE,
            build_subscribe_frame(bot_id, secret, req_id=build_req_id(WS_CMD_SUBSCRIBE)),
            timeout=self._auth_timeout,
        )

    async def _heartbeat_loop(self) -> None:
        while self._connected:
            try:
                await asyncio.sleep(self._heartbeat_interval)
                if not self._connected:
                    return
                await self._request(
                    WS_CMD_HEARTBEAT,
                    build_heartbeat_frame(req_id=build_req_id(WS_CMD_HEARTBEAT)),
                    timeout=self._heartbeat_interval,
                )
                self._missed_heartbeats = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                self._missed_heartbeats += 1
                logger.warning(
                    "Wecom heartbeat failed (%d/%d): instance_id=%s",
                    self._missed_heartbeats,
                    MAX_MISSED_HEARTBEATS,
                    self.instance_id,
                )
                if self._missed_heartbeats >= MAX_MISSED_HEARTBEATS:
                    # 连接已死：交给网关按退避重连，而不是在这里无脑重试
                    self._connected = False
                    transport, self._transport = self._transport, None
                    if transport is not None:
                        with contextlib.suppress(Exception):
                            await transport.close()
                    return

    async def _request(
        self,
        cmd: str,
        frame: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        transport = self._transport
        if transport is None:
            raise ConnectionError("wecom websocket is not connected")
        req_id = str(frame.get("headers", {}).get("req_id") or build_req_id(cmd))
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = future
        try:
            await transport.send_json(frame)
            return await asyncio.wait_for(
                future, timeout=self._ack_timeout if timeout is None else timeout
            )
        finally:
            self._pending.pop(req_id, None)

    async def _receive_loop(self) -> None:
        transport = self._transport
        if transport is None:
            return
        try:
            while True:
                frame = await transport.recv_json()
                self._dispatch_frame(frame)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Wecom receive loop terminated: instance_id=%s",
                self.instance_id,
                exc_info=True,
            )
        finally:
            # 只有仍持有当前 transport 的循环才有资格宣告"连接已死"：重连后
            # 旧循环的收尾不能把新连接的 _connected 又置回 False（否则健康复查
            # 每轮都误判"已死"，实例在 UI 上永远显示未连接）
            if self._transport is transport:
                self._connected = False

    def _dispatch_frame(self, frame: Mapping[str, Any]) -> None:
        cmd = str(frame.get("cmd") or "")
        if cmd == WS_CMD_MSG_CALLBACK:
            message = normalize_callback(frame, instance_id=self.instance_id)
            if message is not None:
                self._schedule_emit(message)
            return
        if cmd == WS_CMD_EVENT_CALLBACK:
            self._handle_event(frame)
            return
        if not cmd:
            # 回执帧：认证 / 心跳 / 出站的统一回执，按 req_id 前缀关联
            self._settle_ack(frame)
            return
        logger.debug(
            "Wecom frame ignored: instance_id=%s cmd=%s", self.instance_id, cmd
        )

    def _settle_ack(self, frame: Mapping[str, Any]) -> None:
        req_id = _req_id_of(frame)
        future = self._pending.get(req_id)
        if future is None or future.done():
            return
        errcode = frame.get("errcode")
        if errcode in (None, 0, "0"):
            future.set_result(dict(frame))
            return
        future.set_exception(
            WecomProtocolError(
                str(errcode), _first_str(frame, ("errmsg", "message")) or "wecom error"
            )
        )

    def _handle_event(self, frame: Mapping[str, Any]) -> None:
        body = frame.get("body")
        event = body.get("event") if isinstance(body, Mapping) else None
        event_type = _first_str(event, ("eventtype", "event_type")) or ""
        if event_type == "disconnected_event":
            # 服务端告知"已有新连接，本连接被顶掉"（官方 SDK 语义：不再自动重连，
            # 因为对方是合法的新连接）。这里除了标记不健康，还必须**收干净旧 socket**：
            # 残留的连接会让我们的下一次 subscribe 又触发一次"新连接建立"，把自己
            # 顶掉，形成每 15s 一次的抢连接循环（消息会随机落在即将被丢弃的 socket 上）。
            logger.warning(
                "Wecom server disconnected this connection (a newer connection exists): "
                "instance_id=%s",
                self.instance_id,
            )
            self._server_disconnected = True
            self._connected = False
            for future in list(self._pending.values()):
                if not future.done():
                    future.set_exception(
                        ConnectionError(
                            "wecom connection was taken over by a newer connection"
                        )
                    )
            self._pending.clear()
            self._track(asyncio.create_task(self._teardown_transport()))
            return
        logger.debug(
            "Wecom event ignored: instance_id=%s eventtype=%s",
            self.instance_id,
            event_type,
        )

    # ==========================================================================
    # 出站
    # ==========================================================================

    async def send_text(self, route: Route, text: str) -> DeliveryResult:
        """主动发送一条文本（单聊的 `chatid` 就是对方 userid）。"""
        try:
            frame = await self._request(
                WS_CMD_SEND,
                build_send_frame(
                    req_id=build_req_id(WS_CMD_SEND),
                    chat_id=route.platform_user_id,
                    text=text,
                ),
            )
        except Exception as exc:
            return self.classify_exception(exc)
        return self._result_from_ack(frame)

    def _result_from_ack(self, frame: Mapping[str, Any]) -> DeliveryResult:
        body = frame.get("body")
        body_map = body if isinstance(body, Mapping) else {}
        ref = _first_str(body_map, ("msgid", "message_id", "id")) or ""
        return DeliveryResult.delivered_with(ref)

    # ==========================================================================
    # 三态归类
    # ==========================================================================

    def classify_exception(self, exc: BaseException) -> DeliveryResult:
        """异常映射表（框架设计 §8.2 的"代价不对称"纪律）。

        - **已知的凭据类错误**：平台确定拒绝，且不会因为重发而成功 -> `rejected`；
        - **其余平台 errcode**：无法确认是否已送达，**不得重发** -> `uncertain`；
        - 超时 / 连接 / 未知异常：同上 -> `uncertain`。

        早期版本把所有 `WecomProtocolError` 都归为 `rejected`，这违反"把 uncertain
        误判为 rejected 会导致重复投递"的不对称纪律，已按本条修正。
        """
        if isinstance(exc, WecomProtocolError):
            if exc.is_credential_error:
                return DeliveryResult.rejected_with(err.CHANNEL_CREDENTIAL_INVALID)
            # errmsg 不丢：它是唯一能说明"为什么被拒"的现场信息（如 40008 不合法的消息类型）
            logger.warning(
                "Wecom delivery rejected by platform: instance_id=%s errcode=%s errmsg=%s",
                self.instance_id,
                exc.code,
                exc.message,
            )
            return DeliveryResult.uncertain_with(f"WECOM_{exc.code}")
        if isinstance(
            exc,
            (
                TimeoutError,
                asyncio.TimeoutError,
                ConnectionError,
                OSError,
                websockets.exceptions.WebSocketException,
            ),
        ):
            return DeliveryResult.uncertain_with(err.CHANNEL_DELIVERY_UNCERTAIN)
        return DeliveryResult.uncertain_with(err.CHANNEL_DELIVERY_UNCERTAIN)

    # ==========================================================================
    # 测试与排障辅助
    # ==========================================================================

    def _schedule_emit(self, message: InboundMessage) -> None:
        self._track(asyncio.create_task(self.emit_inbound(message)))

    def _track(self, task: asyncio.Task[None]) -> None:
        self._inflight_emits.add(task)
        task.add_done_callback(self._inflight_emits.discard)


def _config_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


# ==============================================================================
# 扫码接入驱动（企业微信官方 device flow）
# ==============================================================================


async def _default_http_get(url: str) -> Mapping[str, Any]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.get(url, headers={"accept": "application/json"})
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise WecomProtocolError(
            "HTTP_STATUS", f"qr service returned {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        raise WecomProtocolError("HTTP_ERROR", f"qr request failed: {exc}") from exc
    except ValueError as exc:
        raise WecomProtocolError("BAD_RESPONSE", "qr response is not valid JSON") from exc
    if not isinstance(data, Mapping):
        raise WecomProtocolError("BAD_RESPONSE", "qr response is not an object")
    return data


class WecomProvisioningDriver:
    """企微扫码授权驱动：申请二维码 + 轮询授权结果。

    流程（官方 device flow，**不在 qyapi 上**）：

        GET {base}/ai/qc/generate?source=..&plat=..   -> data.scode / data.auth_url
        GET {base}/ai/qc/query_result?scode=..        -> data.status / data.bot_info.{botid,secret}

    `state` 是平台侧的临时凭据（`scode`），由 `ProvisioningFlow` 加密后落库，
    **绝不出现在任何响应里**；前端只拿到可渲染成二维码的 `auth_url`。
    """

    channel: ClassVar[str] = "wecom_bot"

    def __init__(
        self,
        *,
        api_base: str = QR_SERVICE_BASE,
        http_get: HttpGet | None = None,
        clock: Callable[[], datetime] | None = None,
        source: str = DEFAULT_SOURCE,
        platform: int | None = None,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._http_get = http_get or _default_http_get
        self._clock = clock or (lambda: datetime.now(UTC))
        self._source = source
        self._platform = platform if platform in (1, 2, 3) else default_platform_code()

    async def begin(self) -> ProvisioningSession:
        payload = await self._get(
            QR_GENERATE_PATH,
            {"source": self._source, "plat": str(self._platform)},
        )
        data = _mapping_of(payload.get("data"))
        scode = _first_str(data, ("scode", "state"))
        auth_url = _valid_auth_url(_first_str(data, ("auth_url", "qr_content")))
        if not scode or not auth_url:
            raise WecomProtocolError(
                "BAD_RESPONSE", "qr service response lacks scode or auth_url"
            )
        return ProvisioningSession(
            qr_content=auth_url,
            expires_at=self._clock() + timedelta(seconds=QR_TTL_SECONDS),
            poll_interval_ms=QR_POLL_INTERVAL_MS,
            state=json.dumps({"scode": scode}).encode("utf-8"),
        )

    async def poll(self, state: bytes) -> ProvisioningOutcome:
        try:
            scode = str(json.loads(state.decode("utf-8")).get("scode", ""))
        except (ValueError, UnicodeDecodeError):
            return ProvisioningOutcome(status=STATUS_FAILED, error_code="WECOM_BAD_STATE")
        if not scode:
            return ProvisioningOutcome(status=STATUS_FAILED, error_code="WECOM_BAD_STATE")

        payload = await self._get(QR_POLL_PATH, {"scode": scode})
        errcode = payload.get("errcode")
        if errcode not in (None, 0, "0"):
            return ProvisioningOutcome(
                status=STATUS_FAILED,
                error_code=str(errcode),
            )
        data = _mapping_of(payload.get("data"))
        status = (_first_str(data, ("status", "state")) or "").lower()
        if status in _QR_SUCCESS_STATES:
            info = _mapping_of(data.get("bot_info"))
            bot_id = _first_str(info, ("botid", "bot_id"))
            secret = _first_str(info, ("secret",))
            if not bot_id or not secret:
                return ProvisioningOutcome(
                    status=STATUS_FAILED, error_code="WECOM_MISSING_CREDENTIALS"
                )
            # 只有适配器声明的凭据字段会被落库（其余按密文处理）
            return ProvisioningOutcome(
                status=STATUS_SUCCEEDED,
                credentials={"bot_id": bot_id, "secret": secret},
            )
        if status in _QR_EXPIRED_STATES:
            return ProvisioningOutcome(status=STATUS_EXPIRED)
        if status in _QR_FAILED_STATES:
            return ProvisioningOutcome(
                status=STATUS_FAILED,
                error_code=_first_str(data, ("errmsg", "error_code")) or "WECOM_REJECTED",
            )
        return ProvisioningOutcome(status=STATUS_WAITING)

    async def _get(self, path: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        query = urlencode(dict(params))
        return await self._http_get(f"{self._api_base}{path}?{query}")


def _mapping_of(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _valid_auth_url(value: str | None) -> str | None:
    """`auth_url` 必须是 work.weixin.qq.com 上的 https 地址（官方实现同款校验）。

    把二维码内容限定在官方域名，避免平台被劫持或返回异常值时把用户引导到别处。
    """
    if not value:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != QR_ALLOWED_HOST:
        return None
    if parsed.port not in (None, 443):
        return None
    return value


register_adapter(WecomBotAdapter)
register_driver(WecomProvisioningDriver)

__all__ = [
    "ADAPTER_VERSION",
    "CONVERSATION_TYPE_GROUP_WECOM",
    "CONVERSATION_TYPE_SINGLE",
    "CREDENTIAL_ERROR_CODES",
    "DEFAULT_ACK_TIMEOUT_SECONDS",
    "DEFAULT_AUTH_TIMEOUT_SECONDS",
    "DEFAULT_HEARTBEAT_INTERVAL_SECONDS",
    "DEFAULT_SOURCE",
    "DEFAULT_WS_URL",
    "MAX_MISSED_HEARTBEATS",
    "MAX_TEXT_LENGTH",
    "QR_GENERATE_PATH",
    "QR_POLL_INTERVAL_MS",
    "QR_POLL_PATH",
    "QR_SERVICE_BASE",
    "QR_TTL_SECONDS",
    "WS_CMD_EVENT_CALLBACK",
    "WS_CMD_HEARTBEAT",
    "WS_CMD_MSG_CALLBACK",
    "WS_CMD_RESPOND",
    "WS_CMD_SEND",
    "WS_CMD_SUBSCRIBE",
    "WebSocketWecomTransport",
    "WecomBotAdapter",
    "WecomProtocolError",
    "WecomProvisioningDriver",
    "WecomTransport",
    "build_heartbeat_frame",
    "build_req_id",
    "build_respond_frame",
    "build_send_frame",
    "build_subscribe_frame",
    "default_platform_code",
    "is_ack_frame",
    "normalize_callback",
]
