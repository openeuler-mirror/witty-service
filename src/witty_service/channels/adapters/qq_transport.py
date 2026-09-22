"""QQ 机器人官方开放平台的**原生传输层**：不依赖任何第三方 Python SDK。

## 为什么手写这一层

官方 Python SDK `qq-botpy` 停更在 2024 年的 `1.2.1`，协议在那之后有过变化；它也没有
类型标注，导入期还会执行 `logging.basicConfig` 与 `os.system("")` 污染宿主进程。本层
只在单机器人、单分片场景下工作，真正需要的只有四件——取 token、取网关地址、跑一条
websocket、发一条消息；写清这四件比依赖停更的 SDK 风险更低（断线重连已由渠道网关的
监督循环负责）。

## 协议事实的来源（读的都是官方实现源码，不是猜的）

1. 官方 Python SDK `qq-botpy==1.2.1`：`http.py`（REST 域名与请求头）、`robot.py`
   （`getAppAccessToken` 与 `QQBot <token>` 形式）、`gateway.py`（op 码表、
   identify/resume/心跳载荷、关闭码 4004/9001/9005）、`flags.py`（`Intents.public_messages = 1 << 25`）、
   `api.py`（`/v2/users/{openid}/messages`、`/v2/groups/{group_openid}/messages`、`/gateway/bot`）。
2. 官方 Node SDK `@tencent-connect/qqbot-nodejs@1.0.4`：被动回复体与主动消息体的字段
   差异、`markdownSupport` 默认 `false`、`DEFAULT_CHUNK_LIMIT = 4500`、
   `ReplyLimiter.DEFAULT_LIMIT = 4`。

## 协议摘要

| 步骤 | 请求 | 关键字段 |
|---|---|---|
| 取 token | `POST https://bots.qq.com/app/getAppAccessToken` | `{appId, clientSecret}` -> `{access_token, expires_in}` |
| 调用 REST | `https://api.sgroup.qq.com`（沙箱 `sandbox.api.sgroup.qq.com`） | `Authorization: QQBot <token>` 与 `X-Union-Appid: <appid>` |
| 取网关 | `GET /gateway/bot` | `{url, shards, session_start_limit}` |
| 建连 | `wss` 网关地址 | op10 Hello -> op2 Identify -> READY（`d.session_id`） |
| 保活 | op1 心跳，`d` 是最近一次收到的序号 | op11 为心跳回执 |
| 重连 | op7 服务端要求重连、op9 会话失效 | 关闭码 9001/9005 表示必须重新 Identify |

本层固定 `shard=[0,1]`：QQ 一个机器人默认只有 1 个分片（`shards`），并发数由
`session_start_limit.max_concurrency` 决定；多分片服务的是"同一机器人开多条连接"，当前无此
能力。identify 的 `properties` 跟官方 Node SDK（`$os/$browser/$device`），只是环境信息。

**op6（resume）本层不实现**：9001/9005 明确表示必须重新 Identify，而那只是一次握手，故只保留"断线即重建"一条路径。

**保活必须双向**：只发心跳、不看回执会留下"僵尸连接"（socket 半开时发送仍然成功），于是
`is_alive()` 永远为真、监督循环永不重建、用户消息石沉大海；判据见 `MAX_MISSED_HEARTBEATS`。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import quote

import httpx
from websockets.asyncio.client import connect as ws_connect

from witty_service.channels import errors as err
from witty_service.channels.contracts import (
    CERTAINTY_REJECTED,
    CERTAINTY_UNCERTAIN,
    CONVERSATION_TYPE_GROUP,
)

logger = logging.getLogger(__name__)

# ==============================================================================
# 端点与常量
# ==============================================================================

#: 取 access_token 的地址（固定：沙箱也不影响它）
#  nosec B105：官方公开端点，bandit 只因变量名含 "TOKEN" 而误报
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"  # nosec B105
#: REST 域名：正式与沙箱（官方 SDK 的 Route.DOMAIN / SANDBOX_DOMAIN）
API_BASE = "https://api.sgroup.qq.com"
SANDBOX_API_BASE = "https://sandbox.api.sgroup.qq.com"
#: 取 websocket 网关地址
GATEWAY_PATH = "/gateway/bot"
#: 出站消息路径（单聊用 user_openid，群聊用 group_openid）
C2C_MESSAGE_PATH = "/v2/users/{target_id}/messages"
GROUP_MESSAGE_PATH = "/v2/groups/{target_id}/messages"

#: 公域群 / C2C 消息事件所需的 intent 位（`Intents.public_messages`）
PUBLIC_MESSAGES_INTENT = 1 << 25

#: 网关 op 码表（官方文档与 SDK 的同名常量）
OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

#: 事件名：连接就绪（Identify 被接受后平台下发）
EVENT_READY = "READY"

#: 关闭码 4004：鉴权失败，唯一能确定是"凭据问题"的关闭码；9001/9005 表示必须重新
#: Identify，处理同其余关闭码（断线即重建）。
CLOSE_AUTH_FAILED = 4004

#: 平台消息类型：0 文本 / 2 markdown（官方 API）
MSG_TYPE_TEXT = 0
MSG_TYPE_MARKDOWN = 2

#: token 提前多久刷新：平台给的是"有效期剩余秒数"，留余量避免边界过期
TOKEN_REFRESH_MARGIN_SECONDS = 60.0
#: 单次 HTTP 请求超时（官方 SDK 默认 5s；超时会被判 uncertain，宁可放宽）
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0
#: 建连 + 鉴权就绪的等待上限
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
#: 关闭时等待接收循环退出的上限
CLOSE_GRACE_SECONDS = 1.0
#: Hello 没给心跳间隔时的兜底（毫秒）
DEFAULT_HEARTBEAT_INTERVAL_MS = 30000.0
#: 存活判定容差：连续这么多个心跳间隔没收到任何帧就判定长连接已死。取值与企微
#: `MAX_MISSED_HEARTBEATS` 对齐：容忍 1 次回执丢失，第 2 个间隔仍无声音就断开。
MAX_MISSED_HEARTBEATS = 2


# ==============================================================================
# 错误：三态在传输层就定死
# ==============================================================================


class QqTransportError(Exception):
    """传输层归一化后的平台错误：`certainty` 就是最终的三态，适配器不认识 httpx / websockets
    的异常类型，只按 `certainty` 与 `error_code` 记录投递。"""

    def __init__(self, error_code: str, message: str, *, certainty: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.certainty = certainty


def translate_http_status(status: int, message: str = "") -> QqTransportError:
    """HTTP 状态码 -> 三态（"代价不对称"纪律）。

    代价不对称：把 `uncertain` 误判为 `rejected` 会让上层重发、用户收到两条一样的回答，
    反过来最多是"这条没送达、也没重试"，所以只有平台**明确表示"我没受理"**才返回
    `rejected`：401 鉴权失败（token 取不到 / 不可刷新）、403 / 404 / 429（无权限含主动
    消息额度不足、目标不存在、`msg_id + msg_seq` 重复发送被拒）都是 `rejected`；400 与
    5xx 及其余为 `uncertain`（400 取保守值：请求体由本模块构造且有测试覆盖，真收到 400
    更可能是平台侧策略变化）。
    """
    detail = message or f"the QQ API returned HTTP {status}"
    if status == 401:
        return QqTransportError(
            err.CHANNEL_CREDENTIAL_INVALID, detail, certainty=CERTAINTY_REJECTED
        )
    if status in (403, 404, 429):
        return QqTransportError(f"QQ_{status}", detail, certainty=CERTAINTY_REJECTED)
    if status >= 500:
        return QqTransportError(
            "QQ_SERVER_ERROR", detail, certainty=CERTAINTY_UNCERTAIN
        )
    return QqTransportError(f"QQ_{status}", detail, certainty=CERTAINTY_UNCERTAIN)


def translate_transport_exception(exc: BaseException) -> QqTransportError:
    """网络层异常 -> `uncertain`：超时 / 连接中断时无法确定平台是否已受理，故不重发、不降级。"""
    return QqTransportError(
        err.CHANNEL_DELIVERY_UNCERTAIN,
        f"{exc.__class__.__name__}: {exc}",
        certainty=CERTAINTY_UNCERTAIN,
    )


def translate_close_code(code: int | None) -> QqTransportError | None:
    """websocket 关闭码 -> 错误；没有关闭码（正常关闭）返回 None。"""
    if code is None:
        return None
    if code == CLOSE_AUTH_FAILED:
        return QqTransportError(
            err.CHANNEL_CREDENTIAL_INVALID,
            "the QQ gateway rejected the bot credentials (close code 4004)",
            certainty=CERTAINTY_REJECTED,
        )
    return QqTransportError(
        f"QQ_WS_{code}",
        f"the QQ gateway closed the connection with code {code}",
        certainty=CERTAINTY_UNCERTAIN,
    )


# ==============================================================================
# 载荷构造（纯函数，便于表驱动测试）
# ==============================================================================


def build_identify_payload(
    *,
    token: str,
    intents: int = PUBLIC_MESSAGES_INTENT,
    shard_id: int = 0,
    shard_count: int = 1,
) -> dict[str, Any]:
    """鉴权载荷：`{op:2, d:{shard, token, intents, properties}}`，其中 `token` 必须是平台要求的
    `"QQBot <access_token>"` 前缀形式（官方 SDK 的 `Token.get_string()`），不是裸 token。
    """
    return {
        "op": OP_IDENTIFY,
        "d": {
            "shard": [shard_id, shard_count],
            "token": f"QQBot {token}",
            "intents": intents,
            "properties": {
                "$os": "linux",
                "$browser": "witty-service",
                "$device": "witty-service",
            },
        },
    }


def build_heartbeat_payload(seq: int = 0) -> dict[str, Any]:
    """心跳：`{op:1, d:<最近一次收到的序号>}`（还没收到过事件时是 0）。"""
    return {"op": OP_HEARTBEAT, "d": seq}


def build_message_body(
    *,
    text: str,
    markdown: bool = False,
    msg_id: str | None = None,
    msg_seq: int | None = None,
) -> dict[str, Any]:
    """出站请求体：被动回复带 `msg_id + msg_seq`，主动消息两者都不带。

    被动回复必须基于**同一条**入站消息，5 分钟内有效、最多 4 条；主动消息带上这两个
    字段会被平台当成被动回复，"额度用尽后的降级"反而持续失败（官方 Node SDK 为此拆成
    `buildMessageBody` / `buildProactiveBody`）。
    """
    body: dict[str, Any] = (
        {"markdown": {"content": text}, "msg_type": MSG_TYPE_MARKDOWN}
        if markdown
        else {"content": text, "msg_type": MSG_TYPE_TEXT}
    )
    if msg_id:
        body["msg_id"] = msg_id
        body["msg_seq"] = msg_seq if msg_seq is not None else 1
    return body


def message_path(*, conversation_type: str, target_id: str) -> str:
    """出站路径：单聊用 `user_openid`，群聊用 `group_openid`。

    `target_id` 来自平台，但仍要转义后再拼进路径：带 `/` 或 `?` 的取值会把请求打到**别的**
    路径上（`/v2/users/a/b?x=1/messages` 会被解析成别的路径 + 查询串），结果是"静默发错地方"。
    """
    template = (
        GROUP_MESSAGE_PATH
        if conversation_type == CONVERSATION_TYPE_GROUP
        else C2C_MESSAGE_PATH
    )
    return template.format(target_id=quote(target_id, safe=""))


# ==============================================================================
# REST 客户端
# ==============================================================================


class QqApiClient:
    """官方 REST 客户端：取 token、取网关地址、发消息；token 缓存到"有效期 - 60s"，并发刷新
    由锁串行化。`http_client` 可注入，测试用 `httpx.MockTransport` 覆盖真实 HTTP 语义。
    """

    def __init__(
        self,
        *,
        app_id: str,
        secret: str,
        sandbox: bool = False,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT_SECONDS,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._app_id = app_id
        self._secret = secret
        self._api_base = SANDBOX_API_BASE if sandbox else API_BASE
        self._http_timeout = http_timeout
        self._http = http_client
        self._owns_http = http_client is None
        self._clock = clock or time.monotonic
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    # ------------------------------------------------------------------ HTTP

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self._http_timeout)
        return self._http

    async def aclose(self) -> None:
        """关闭自建的连接池（注入进来的客户端归调用方管）。"""
        http, self._http = self._http, None
        if http is not None and self._owns_http:
            with contextlib.suppress(Exception):
                await http.aclose()

    # ----------------------------------------------------------------- token

    async def access_token(self) -> str:
        """取（或复用）access_token；凭据不正确时抛 `rejected`：平台对错误凭据返回 200 +
        错误体（官方 SDK 只能抛裸 RuntimeError），所以"没有 access_token"要自己判成凭据问题。
        """
        async with self._token_lock:
            if self._token and self._clock() < self._token_expires_at:
                return self._token
            payload = await self._request_json(
                TOKEN_URL,
                {"appId": self._app_id, "clientSecret": self._secret},
                authenticated=False,
            )
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise QqTransportError(
                    err.CHANNEL_CREDENTIAL_INVALID,
                    "the QQ token endpoint did not return an access_token",
                    certainty=CERTAINTY_REJECTED,
                )
            self._token = token
            self._token_expires_at = self._clock() + self._ttl_seconds(payload)
            return token

    def _ttl_seconds(self, payload: Mapping[str, Any]) -> float:
        """把 `expires_in` 换成"还能安全用多久"（秒）：平台这里的类型不稳定（数字与字符串
        都出现过），两种都接，其余按"立刻过期"处理（宁可多要一次 token）。
        """
        raw = payload.get("expires_in")
        seconds = 0.0
        if isinstance(raw, bool):
            seconds = 0.0
        elif isinstance(raw, (int, float)):
            seconds = float(raw)
        elif isinstance(raw, str) and raw.strip():
            try:
                seconds = float(raw.strip())
            except ValueError:
                seconds = 0.0
        return max(0.0, seconds - TOKEN_REFRESH_MARGIN_SECONDS)

    async def _invalidate_token(self) -> None:
        """丢弃缓存的 access_token：平台已明确表示不认它（HTTP 401）。与"提前 60s 刷新"
        是两件事：平台提前作废、密钥轮转、多实例顶号都会让还没到期的 token 直接失效，
        不丢缓存则整个 TTL（最长 2h）内每条出站都必然 401。
        """
        async with self._token_lock:
            self._token = None
            self._token_expires_at = 0.0

    # -------------------------------------------------------------- 业务调用

    async def gateway_url(self) -> str:
        """取 websocket 网关地址（`GET /gateway/bot`）。"""
        payload = await self._request("GET", GATEWAY_PATH)
        url = payload.get("url")
        if not isinstance(url, str) or not url:
            raise QqTransportError(
                "QQ_BAD_GATEWAY",
                "the QQ gateway endpoint did not return a url",
                certainty=CERTAINTY_UNCERTAIN,
            )
        return url

    async def send_message(
        self,
        *,
        conversation_type: str,
        target_id: str,
        text: str,
        markdown: bool = False,
        msg_id: str | None = None,
        msg_seq: int | None = None,
    ) -> Mapping[str, Any]:
        """发一条消息，返回平台响应（至少含 `id`）。"""
        body = build_message_body(
            text=text, markdown=markdown, msg_id=msg_id, msg_seq=msg_seq
        )
        path = message_path(conversation_type=conversation_type, target_id=target_id)
        return await self._request("POST", path, payload=body)

    # ---------------------------------------------------------------- 内部实现

    async def _request(
        self, method: str, path: str, *, payload: Mapping[str, Any] | None = None
    ) -> Mapping[str, Any]:
        return await self._request_json(
            f"{self._api_base}{path}", payload, method=method, authenticated=True
        )

    async def _request_json(
        self,
        url: str,
        payload: Mapping[str, Any] | None,
        *,
        method: str = "POST",
        authenticated: bool = True,
        retry_on_unauthorized: bool = True,
    ) -> Mapping[str, Any]:
        headers: dict[str, str] = {"accept": "application/json"}
        if authenticated:
            token = await self.access_token()
            headers["Authorization"] = f"QQBot {token}"
            headers["X-Union-Appid"] = self._app_id
        try:
            response = await self._client().request(
                method, url, json=dict(payload or {}), headers=headers
            )
        except httpx.HTTPError as exc:
            raise translate_transport_exception(exc) from exc
        if (
            response.status_code == httpx.codes.UNAUTHORIZED
            and authenticated
            and retry_on_unauthorized
        ):
            # 平台说这个 token 不认：**先丢缓存再原样重试一次**。重试只做一次是为了
            # 不把"凭据真的错了"变成死循环；第二次仍是 401 就按凭据问题上报。
            await self._invalidate_token()
            logger.info("QQ access token was rejected (HTTP 401); refetching once")
            return await self._request_json(
                url,
                payload,
                method=method,
                authenticated=authenticated,
                retry_on_unauthorized=False,
            )
        if response.status_code != httpx.codes.OK:
            raise translate_http_status(response.status_code, _error_message(response))
        try:
            data = response.json()
        except ValueError as exc:
            # 2xx 但响应不是 JSON：无法确认平台是否受理 -> uncertain
            raise QqTransportError(
                err.CHANNEL_DELIVERY_UNCERTAIN,
                "the QQ API returned a non-JSON response",
                certainty=CERTAINTY_UNCERTAIN,
            ) from exc
        if not isinstance(data, Mapping):
            raise QqTransportError(
                err.CHANNEL_DELIVERY_UNCERTAIN,
                "the QQ API returned an unexpected payload",
                certainty=CERTAINTY_UNCERTAIN,
            )
        return data


def _error_message(response: httpx.Response) -> str:
    """平台错误体是 `{code, message}`（官方 SDK 只读 `message`）。"""
    try:
        data = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    if isinstance(data, Mapping):
        message = data.get("message") or data.get("msg")
        code = data.get("code")
        if isinstance(message, str) and message:
            return f"{message} (code={code})" if code is not None else message
    return f"HTTP {response.status_code}"


# ==============================================================================
# 传输层（websocket 长连接）
# ==============================================================================

WsFactory = Callable[[str], Awaitable[Any]]


class QqGatewayTransport:
    """官方网关的 websocket 长连接：Hello -> Identify -> READY -> 心跳 + 派发事件。

    **不做内部重连**：断开就让接收循环结束、`is_alive()` 变 False，由渠道网关的监督循环
    按退避重建（与企微同模型）；两处各自重连会互相打架，且平台侧多连接会互相顶号。
    """

    def __init__(
        self,
        *,
        app_id: str,
        secret: str,
        sandbox: bool = False,
        api: QqApiClient | None = None,
        ws_factory: WsFactory | None = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        heartbeat_interval_ms: float = DEFAULT_HEARTBEAT_INTERVAL_MS,
        intents: int = PUBLIC_MESSAGES_INTENT,
    ) -> None:
        self._app_id = app_id
        self._secret = secret
        self._api = api or QqApiClient(app_id=app_id, secret=secret, sandbox=sandbox)
        self._ws_factory = ws_factory or _default_ws_factory
        self._connect_timeout = connect_timeout
        self._heartbeat_interval_ms = heartbeat_interval_ms
        self._intents = intents
        self._ws: Any = None
        self._task: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._ready: asyncio.Event | None = None
        self._failure: QqTransportError | None = None
        self._handler: Callable[[Mapping[str, Any]], None] | None = None
        self._session_id = ""
        self._last_seq = 0
        self._established = False
        #: 最近一次收到**任何**帧的时刻（monotonic）：存活判定唯一的依据
        self._last_frame_at = time.monotonic()

    # ------------------------------------------------------------- 生命周期

    def on_event(self, handler: Callable[[Mapping[str, Any]], None]) -> None:
        self._handler = handler

    def is_alive(self) -> bool:
        task = self._task
        ready = self._ready
        return bool(
            self._failure is None
            and ready is not None
            and ready.is_set()
            and task is not None
            and not task.done()
        )

    async def connect(self) -> None:
        """建连并等到 READY；失败时抛 `QqTransportError`（三态已定）。

        **只有"没走到 READY"才算建连失败**：READY 之后的断开（重连要求、会话失效、网络抖动）
        由 `is_alive()` 变 False 表达、交给监督循环重连，否则每条重连要求都会让 `start()` 抛异常。
        """
        if not self._app_id or not self._secret:
            raise QqTransportError(
                err.CHANNEL_CREDENTIAL_INVALID,
                "app_id and secret are both required for the QQ bot",
                certainty=CERTAINTY_REJECTED,
            )
        self._ready = asyncio.Event()
        self._failure = None
        self._established = False
        self._last_frame_at = time.monotonic()
        self._task = asyncio.create_task(self._run())
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self._connect_timeout)
        except TimeoutError as exc:
            await self.close()
            raise QqTransportError(
                "QQ_CONNECT_TIMEOUT",
                f"the QQ gateway did not become ready in {self._connect_timeout:.0f}s",
                certainty=CERTAINTY_UNCERTAIN,
            ) from exc
        except asyncio.CancelledError:
            # 取消路径也要收干净：监督循环的 stop() 可能正停在这里等 READY，只把
            # CancelledError 抛出去会留下僵尸连接、一直占着平台的单连接名额（清理用 shield）。
            with contextlib.suppress(BaseException):
                await asyncio.shield(self.close())
            raise
        if self._failure is not None and not self._established:
            failure = self._failure
            await self.close()
            raise failure

    async def close(self) -> None:
        await self._stop_heartbeat()
        ws, self._ws = self._ws, None
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()
        task, self._task = self._task, None
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=CLOSE_GRACE_SECONDS
                )
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("QQ receive loop ended with an error", exc_info=True)
        self._ready = None
        self._session_id = ""
        await self._api.aclose()

    # ------------------------------------------------------------------ 出站

    async def send_message(
        self,
        *,
        conversation_type: str,
        target_id: str,
        text: str,
        msg_id: str | None = None,
        msg_seq: int | None = None,
        markdown: bool = False,
    ) -> Mapping[str, Any]:
        """发一条消息：**只经 REST，不经 websocket**；平台没有"发出去就一定有回执"的长
        连接通道，HTTP 响应才是唯一凭据，因此不缓存、不排队，失败直接抛给适配器归类三态。
        """
        return await self._api.send_message(
            conversation_type=conversation_type,
            target_id=target_id,
            text=text,
            markdown=markdown,
            msg_id=msg_id,
            msg_seq=msg_seq,
        )

    # ------------------------------------------------------------ 连接内部

    async def _run(self) -> None:
        """建连 -> 鉴权 -> 收事件；任何失败都记进 `_failure` 并结束循环。"""
        try:
            token = await self._api.access_token()
            url = await self._api.gateway_url()
            self._ws = await self._ws_factory(url)
            interval = await self._handshake(token)
            self._established = True
            self._set_ready()
            self._heartbeat = asyncio.create_task(self._heartbeat_loop(interval))
            await self._receive_loop()
        except asyncio.CancelledError:
            raise
        except QqTransportError as exc:
            self._set_failure(exc)
            logger.warning(
                "QQ gateway session ended: app_id=%s session_id=%s "
                "error_code=%s message=%s",
                self._masked_app_id(),
                self._session_id or "-",
                exc.error_code,
                exc.message,
            )
        except Exception as exc:  # pragma: no cover - 兜底：异常类型不可枚举
            self._set_failure(translate_transport_exception(exc))
            logger.warning(
                "QQ gateway session failed: app_id=%s error=%s",
                self._masked_app_id(),
                exc.__class__.__name__,
                exc_info=True,
            )
        finally:
            # 就绪事件必须放行：`connect()` 在等它，失败也必须让它返回
            self._set_ready()

    async def _handshake(self, token: str) -> float:
        """收 Hello、发 Identify、等 READY；返回心跳间隔（秒）。"""
        hello = await self._recv_frame()
        if hello.get("op") != OP_HELLO:
            raise QqTransportError(
                "QQ_BAD_HELLO",
                f"the QQ gateway did not send Hello first (got {hello!r})",
                certainty=CERTAINTY_UNCERTAIN,
            )
        await self._send_frame(
            build_identify_payload(token=token, intents=self._intents)
        )
        interval_ms = _heartbeat_interval_ms(hello) or self._heartbeat_interval_ms
        ready = await self._await_ready(interval_ms)
        self._session_id = _str_field(ready, "session_id")
        return max(interval_ms, 1000.0) / 1000.0

    async def _await_ready(self, interval_ms: float) -> Mapping[str, Any]:
        """等 READY：Identify 与 READY 之间平台仍要求保活，所以按 Hello 给的间隔自己补
        心跳，否则握手慢过间隔时平台会按"未心跳"断开。
        """
        deadline = time.monotonic() + self._connect_timeout
        next_beat = time.monotonic() + interval_ms / 1000.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QqTransportError(
                    "QQ_CONNECT_TIMEOUT",
                    "the QQ gateway did not send READY after Identify",
                    certainty=CERTAINTY_UNCERTAIN,
                )
            timeout = min(remaining, max(0.0, next_beat - time.monotonic()))
            try:
                frame = await asyncio.wait_for(self._recv_frame(), timeout=timeout)
            except TimeoutError:
                if time.monotonic() >= deadline:
                    continue
                await self._send_frame(build_heartbeat_payload(self._last_seq))
                next_beat = time.monotonic() + interval_ms / 1000.0
                continue
            if _event_type(frame) == EVENT_READY:
                body = frame.get("d")
                return body if isinstance(body, Mapping) else {}
            # READY 之前可能夹着心跳回执或"要求重连"：交给 `_dispatch` 统一判定
            self._dispatch(frame)

    async def _receive_loop(self) -> None:
        while True:
            frame = await self._recv_frame()
            self._dispatch(frame)

    def _dispatch(self, frame: Mapping[str, Any]) -> None:
        op = frame.get("op")
        seq = frame.get("s")
        if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0:
            # 心跳的 `d` 必须回带最近收到的序号，否则平台会按无效心跳处理
            self._last_seq = seq
        if op == OP_DISPATCH:
            if _event_type(frame) == EVENT_READY:
                body = frame.get("d")
                self._session_id = _str_field(
                    body if isinstance(body, Mapping) else {}, "session_id"
                )
                return
            self._emit(frame)
            return
        if op == OP_HEARTBEAT_ACK:
            # 回执不带信息，但收帧本身已把时刻记进 `_last_frame_at`（见 `_heartbeat_loop`）
            return
        if op == OP_RECONNECT:
            # 平台要求重建连接：结束本连接，交给渠道网关的监督循环
            raise QqTransportError(
                "QQ_WS_RECONNECT",
                "the QQ gateway asked the client to reconnect",
                certainty=CERTAINTY_UNCERTAIN,
            )
        if op == OP_INVALID_SESSION:
            # 会话失效：必须重新 Identify（本层重连即重新鉴权，不做 resume）
            raise QqTransportError(
                "QQ_WS_INVALID_SESSION",
                "the QQ gateway invalidated the session",
                certainty=CERTAINTY_UNCERTAIN,
            )
        logger.debug("QQ frame ignored: op=%s type=%s", op, _event_type(frame))

    async def _heartbeat_loop(self, interval_seconds: float) -> None:
        """保活 + 存活判定：**只看回执不够，还得看有没有人应声**——半开的 socket 上发送仍然
        成功（数据只进本地缓冲区），所以「心跳发出去了」证明不了链路可用；判据是距上次收到
        **任何**帧超过 `MAX_MISSED_HEARTBEATS` 个间隔（`_fail` 立刻把 `is_alive()` 打成假）。
        """
        tolerance = interval_seconds * MAX_MISSED_HEARTBEATS
        while True:
            await asyncio.sleep(interval_seconds)
            silent = time.monotonic() - self._last_frame_at
            if silent >= tolerance:
                await self._fail(
                    QqTransportError(
                        "QQ_WS_HEARTBEAT_TIMEOUT",
                        f"the QQ gateway sent nothing for {silent:.0f}s "
                        f"(heartbeat interval {interval_seconds:.0f}s)",
                        certainty=CERTAINTY_UNCERTAIN,
                    )
                )
                return
            await self._send_frame(build_heartbeat_payload(self._last_seq))

    async def _stop_heartbeat(self) -> None:
        heartbeat, self._heartbeat = self._heartbeat, None
        if heartbeat is not None and not heartbeat.done():
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    # ------------------------------------------------------------- 帧收发

    async def _send_frame(self, payload: Mapping[str, Any]) -> None:
        ws = self._ws
        if ws is None:
            raise QqTransportError(
                "QQ_WS_CLOSED",
                "the QQ websocket is not connected",
                certainty=CERTAINTY_UNCERTAIN,
            )
        await ws.send(json.dumps(payload, ensure_ascii=False))

    async def _recv_frame(self) -> Mapping[str, Any]:
        ws = self._ws
        if ws is None:
            raise QqTransportError(
                "QQ_WS_CLOSED",
                "the QQ websocket is not connected",
                certainty=CERTAINTY_UNCERTAIN,
            )
        try:
            raw = await ws.recv()
        except Exception as exc:
            raise self._close_error(exc) from exc
        # 收到任何字节都算"链路还活着"（心跳回执、事件、平台要求的重连帧都算）
        self._last_frame_at = time.monotonic()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise QqTransportError(
                "QQ_BAD_FRAME",
                "the QQ gateway sent a non-JSON frame",
                certainty=CERTAINTY_UNCERTAIN,
            ) from exc
        if not isinstance(data, Mapping):
            raise QqTransportError(
                "QQ_BAD_FRAME",
                "the QQ gateway sent an unexpected frame",
                certainty=CERTAINTY_UNCERTAIN,
            )
        return data

    def _close_error(self, exc: BaseException) -> QqTransportError:
        """websocket 关闭 -> 官方错误码（4004 是凭据问题，其余按连接问题处理）。"""
        code = _close_code(exc)
        translated = translate_close_code(code)
        if translated is not None:
            return translated
        return translate_transport_exception(exc)

    # ------------------------------------------------------------------ 工具

    def _emit(self, frame: Mapping[str, Any]) -> None:
        handler = self._handler
        if handler is None:
            return
        try:
            handler(frame)
        except Exception:  # pragma: no cover - 上报失败不该打断接收循环
            logger.exception("QQ inbound frame could not be handed over")

    def _set_ready(self) -> None:
        ready = self._ready
        if ready is not None:
            ready.set()

    def _set_failure(self, exc: QqTransportError) -> None:
        """记下失败原因；**保留第一条**——后续往往只是它的次生现象：例如心跳超时后主动关掉
        socket，接收循环随即抛出的"连接已关闭"会盖掉真正的原因。
        """
        if self._failure is None:
            self._failure = exc

    async def _fail(self, exc: QqTransportError) -> None:
        """就地把这条连接判死：记下原因并关掉 websocket，好让**阻塞在 `recv()` 上的接收循环
        醒过来**（异常只留在心跳任务里没人看，它会一直等在死 socket 上，`is_alive()` 也就一直为真）。
        """
        self._set_failure(exc)
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close()

    def _masked_app_id(self) -> str:
        value = self._app_id
        if len(value) < 8:
            return "*" * len(value)
        return f"{value[:4]}****{value[-4:]}"


async def _default_ws_factory(url: str) -> Any:
    # ping_interval=None：保活完全走平台自己的 op1 心跳，避免两套保活互相干扰
    return await ws_connect(
        url, open_timeout=DEFAULT_HTTP_TIMEOUT_SECONDS, ping_interval=None
    )


def _event_type(frame: Mapping[str, Any]) -> str:
    value = frame.get("t")
    return value.upper() if isinstance(value, str) else ""


def _str_field(source: Mapping[str, Any], key: str) -> str:
    value = source.get(key)
    return value if isinstance(value, str) else ""


def _heartbeat_interval_ms(hello: Mapping[str, Any]) -> float:
    body = hello.get("d")
    if not isinstance(body, Mapping):
        return 0.0
    value = body.get("heartbeat_interval")
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return float(value)
    return 0.0


def _close_code(exc: BaseException) -> int | None:
    """从 websockets 的关闭异常里取关闭码（不同版本的字段名不一样）。"""
    for attr in ("code", "rcvd", "sent"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        code = getattr(value, "code", None)
        if isinstance(code, int) and not isinstance(code, bool):
            return code
    return None


__all__ = [
    "API_BASE",
    "C2C_MESSAGE_PATH",
    "CLOSE_AUTH_FAILED",
    "CLOSE_GRACE_SECONDS",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_HEARTBEAT_INTERVAL_MS",
    "DEFAULT_HTTP_TIMEOUT_SECONDS",
    "EVENT_READY",
    "GATEWAY_PATH",
    "GROUP_MESSAGE_PATH",
    "MAX_MISSED_HEARTBEATS",
    "MSG_TYPE_MARKDOWN",
    "MSG_TYPE_TEXT",
    "OP_DISPATCH",
    "OP_HEARTBEAT",
    "OP_HEARTBEAT_ACK",
    "OP_HELLO",
    "OP_IDENTIFY",
    "OP_INVALID_SESSION",
    "OP_RECONNECT",
    "PUBLIC_MESSAGES_INTENT",
    "SANDBOX_API_BASE",
    "TOKEN_REFRESH_MARGIN_SECONDS",
    "TOKEN_URL",
    "QqApiClient",
    "QqGatewayTransport",
    "QqTransportError",
    "build_heartbeat_payload",
    "build_identify_payload",
    "build_message_body",
    "message_path",
    "translate_close_code",
    "translate_http_status",
    "translate_transport_exception",
]
