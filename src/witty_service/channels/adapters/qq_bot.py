"""QQ 机器人适配器：官方开放平台之上的渠道适配。

## 协议事实的来源（读自官方实现源码）

1. **传输层是自研的**（`qq_transport.py`），不依赖第三方 Python SDK：官方 `qq-botpy`
   最后发布于 2024 年的 `1.2.1`（无类型标注、导入期执行 `logging.basicConfig`、
   `BotHttp.request` **超时返回 None**）。协议细节与来源见该模块 docstring，本模块只
   消费 `connect/close/is_alive/send_message` 与"异常 -> 三态"。
2. 官方 Node SDK `@tencent-connect/qqbot-nodejs@1.0.4` 把平台约束写成了可执行代码，本
   适配器照抄这些取值：
   - `protocol/utils/reply-limiter`：**一条入站消息最多 4 条被动回复**（`DEFAULT_LIMIT
     = 4`）；官方文档的完整规则是**单聊 60 分钟 / 4 次、群聊 5 分钟 / 5 次**；
   - `protocol/api/messages.buildMessageBody / buildProactiveBody`：被动回复体是
     `{content|markdown, msg_type, msg_seq, msg_id}`；**主动消息体既没有 msg_id，也没有
     msg_seq**；
   - `USAGE.md`：被动回复必须基于 **5 分钟内**的入站消息；`markdownSupport` 默认为
     `false`（无 markdown 权限时平台以 `40034090` 拒绝），故本适配器默认发纯文本；
   - `protocol/types.d.ts`：`C2C_MESSAGE_CREATE.d` 是
     `{id, content, timestamp, author:{user_openid}, attachments, msg_elements}`；
     `GROUP_AT_MESSAGE_CREATE.d` 另有 `group_openid` 与 `mentions`——群里那条 @机器人由
     `mentions` 承载、**正文不含它**，故这里不做剥离前导 @ 的处理。
3. 官方扫码连接器 `@tencent-connect/qqbot-connector@1.2.0`：绑定任务的端点与密文解法见
   `QqProvisioningDriver`。

## 被动回复与主动消息

出站统一是 `POST /v2/users/{openid}/messages`（群聊 `/v2/groups/{group_openid}/messages`）：
被动回复体带 `msg_id` + `msg_seq`，主动消息体两者都没有；窗口与条数见 `PASSIVE_REPLY_RULES`
与 `MAX_REPLY_SEGMENTS`（官方文档《消息收发概述 · 频率与时效规则》）。主动消息受平台额度
限制，且**用户可在客户端关闭**，关掉后一律失败；窗口取官方值而非收紧——收紧到 5 分钟会让
长回合的终稿退化成主动消息。

适配器按路由记住最近一条入站消息的 `msg_id` 与投递目标（单聊 user_openid、群聊
group_openid）：窗口内且额度未用尽 -> 发被动回复，`msg_seq` 在同一个 `msg_id` 内递增（平台
按 `msg_id + msg_seq` 判重）；窗口过期或额度用尽 -> **退化为主动消息**（本地即可判定的确定性
降级）；结果不确定（超时 / 5xx / 未知异常）-> 一律 `uncertain`，**不重发、不降级**——当成
"没送到"重发会让用户收到两条一样的回答。

## 接入方式：扫码（主路径）与手填（救援路径）

扫码走**官方 q.qq.com「lite 绑定任务」**（`QqProvisioningDriver`）：端点、字段与 AES-256-GCM
密文解法见该驱动的 docstring；二维码过期时驱动**换一个新任务**并把新二维码交回编排层，一次
接入因此能跨多次刷新，界面表现为"二维码自动换了一张"。

手填凭据（`POST /channels/instances`）与扫码共用同一套落库与回滚逻辑，既是扫码不可用时的救援
通道，也是凭据遗失后的重新接入方式；`/channels/catalog` 里 `supports_provisioning=true`。

绑定结果里还带**扫码人的 openid**（`user_openid`）——四个渠道里唯一能拿到扫码人身份的渠道。
它写进实例的非密配置 `owner_user_openid`：实例详情可见，也能直接填进"连通性测试"的接收用户
ID。**不**用它自动收紧准入策略——准入默认"放开"是产品决定。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, Protocol
from urllib.parse import urlencode
from uuid import uuid4

from witty_service.channels import errors as err
from witty_service.channels.adapters.base import BaseChannelAdapter, CredentialField
from witty_service.channels.adapters.qq_transport import (
    MSG_TYPE_MARKDOWN,
    MSG_TYPE_TEXT,
    QqGatewayTransport,
    QqTransportError,
)
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

ADAPTER_VERSION = "0.1.0"

#: 单条文本上限：与官方 Node SDK 的分片上限一致（`DEFAULT_CHUNK_LIMIT = 4500`）
MAX_TEXT_LENGTH = 4500
#: 一条入站消息允许的**总出站条数**（占位消息 + 终稿分段）：取单聊 4 / 群聊 5 的下限，
#: 因为 `delivery.plan` 不认识会话场景。
#: 「占位消息也算一条」由路由器保证：`SessionRouter._deliver_final` 把本回合已出站的条数
#: （占位消息、停滞提示）作为 `reserved` 交给 `delivery.plan` 扣减；否则 4 条分段加占位
#: 就是 5 条出站，第 5 条会降级成需用户先授权的主动消息而大概率被拒。
MAX_REPLY_SEGMENTS = 4
#: 被动回复规则（官方文档《消息收发概述 · 频率与时效规则》）：
#: **单聊 60 分钟 / 4 次，群聊 5 分钟 / 5 次**。
PASSIVE_REPLY_RULES: Mapping[str, tuple[float, int]] = {
    CONVERSATION_TYPE_DIRECT: (3600.0, 4),
    CONVERSATION_TYPE_GROUP: (300.0, 5),
}
#: 场景取值异常时的兜底规则：取更短窗口 + 更小次数（宁可多降级，不可撞平台上限）
PASSIVE_REPLY_FALLBACK_RULE: tuple[float, int] = (300.0, 4)
#: 回复上下文的保留时长与条数上限（只用来记住 msg_id 与投递目标，过期即清）
CONTEXT_RETENTION_SECONDS = 3600.0
MAX_TRACKED_CONTEXTS = 1000
#: 等待长连接完成鉴权并收到 READY 的超时（秒）
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0

#: 网关事件类型（官方网关常量）
EVENT_C2C_MESSAGE_CREATE = "C2C_MESSAGE_CREATE"
EVENT_GROUP_AT_MESSAGE_CREATE = "GROUP_AT_MESSAGE_CREATE"

#: 附件 content_type 前缀 -> 不支持内容类型
_ATTACHMENT_KINDS = (
    ("image", UNSUPPORTED_IMAGE),
    ("voice", UNSUPPORTED_VOICE),
    ("audio", UNSUPPORTED_VOICE),
    ("video", UNSUPPORTED_FILE),
    ("file", UNSUPPORTED_FILE),
)


# ==============================================================================
# 归一化（纯函数：官方网关线格式 -> InboundMessage）
# ==============================================================================


def normalize_event(
    frame: Mapping[str, Any], *, instance_id: str
) -> InboundMessage | None:
    """把一条网关事件归一化为 `InboundMessage`；不是用户消息的事件返回 None。

    入参是**官方网关线格式** `{"id": 事件 id, "op": 0, "t": 事件类型, "d": {...}}`；
    传输层不做字段搬运，因此本函数是纯函数，可用录制帧做表驱动测试。只处理
    `C2C_MESSAGE_CREATE`（私聊）与 `GROUP_AT_MESSAGE_CREATE`（群里 @机器人），其余返回 None。
    """
    event_type = str(frame.get("t") or "").upper()
    if event_type not in (EVENT_C2C_MESSAGE_CREATE, EVENT_GROUP_AT_MESSAGE_CREATE):
        return None
    body = frame.get("d")
    if not isinstance(body, Mapping):
        return None

    is_group = event_type == EVENT_GROUP_AT_MESSAGE_CREATE
    author = body.get("author")
    author_map = author if isinstance(author, Mapping) else {}
    sender = _str_of(author_map, "member_openid" if is_group else "user_openid")

    # 缺事件标识时用随机标识兜底去重，避免两条消息被合成一条
    event_id = _str_of(body, "id") or _str_of(frame, "id") or f"fallback-{uuid4().hex}"

    text, unsupported_kind = _content_of(body)
    return InboundMessage(
        platform_event_id=event_id,
        route=Route(
            instance_id=instance_id,
            conversation_type=(
                CONVERSATION_TYPE_GROUP if is_group else CONVERSATION_TYPE_DIRECT
            ),
            platform_user_id=sender,
        ),
        text=text,
        unsupported_kind=unsupported_kind,
        received_at=_received_at(body),
    )


def outbound_target(frame: Mapping[str, Any]) -> str:
    """出站地址：单聊是提问者 `user_openid`，群聊是 `group_openid`。

    路由键只有「实例 + 会话类型 + 用户」三个字段，群聊的 `group_openid` 放不进去，因此在
    入站时单独取出、随回复上下文一起记住。
    """
    body = frame.get("d")
    if not isinstance(body, Mapping):
        return ""
    if str(frame.get("t") or "").upper() == EVENT_GROUP_AT_MESSAGE_CREATE:
        return _str_of(body, "group_openid")
    author = body.get("author")
    author_map = author if isinstance(author, Mapping) else {}
    return _str_of(author_map, "user_openid")


def _content_of(body: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """正文与"不支持的内容类型"：不支持的内容**也要上报**（text=None + kind）。

    语音的 ASR 转写（`attachments[].asr_refer_text`）有值就当文本处理（与企微同取舍）。
    """
    content = _str_of(body, "content")
    if content:
        return content, None
    attachments = body.get("attachments")
    items = attachments if isinstance(attachments, list) else []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        transcript = _str_of(item, "asr_refer_text")
        if transcript:
            return transcript, None
    if not items:
        # 既无正文也无附件：按空文本上报（是否算"空消息"由上层决定）
        return "", None
    first = items[0] if isinstance(items[0], Mapping) else {}
    return None, _attachment_kind(_str_of(first, "content_type"))


def _attachment_kind(content_type: str) -> str:
    lowered = content_type.lower()
    for prefix, kind in _ATTACHMENT_KINDS:
        if lowered.startswith(prefix):
            return kind
    return UNSUPPORTED_UNKNOWN


def _received_at(body: Mapping[str, Any]) -> datetime:
    """平台时间戳既可能是毫秒数（字符串或数字），也可能是 ISO8601 字符串。"""
    raw = body.get("timestamp")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
        return _from_epoch(float(raw))
    if isinstance(raw, str) and raw.strip():
        value = raw.strip()
        if value.isdigit():
            return _from_epoch(float(value))
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _from_epoch(value: float) -> datetime:
    seconds = value / 1000.0 if value > 1e11 else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return datetime.now(UTC)


def _first_str(source: object, keys: tuple[str, ...]) -> str | None:
    """按顺序取第一个非空字段值：平台换过字段名（`bot_appid` vs `bot_app_id`），
    因此关键字段都按候选名依次取。
    """
    if not isinstance(source, Mapping):
        return None
    for key in keys:
        value = _str_of(source, key)
        if value:
            return value
    return None


def _str_of(source: Mapping[str, Any], key: str) -> str:
    value = source.get(key)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


# ==============================================================================
# 传输层接口（实现在 `qq_transport.py`：官方协议的原生实现）
# ==============================================================================


class QqTransport(Protocol):
    """传输层接口：把"官方协议"隔离在一个可替换的薄层里。

    `send_message` 返回平台响应（至少含 `id`）；异常一律是 `QqTransportError`（三态已
    定），"没有响应"直接归类为 `uncertain` 抛出，让"不确定"只有一个来源。
    """

    def on_event(self, handler: Callable[[Mapping[str, Any]], None]) -> None: ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    def is_alive(self) -> bool: ...

    async def send_message(
        self,
        *,
        conversation_type: str,
        target_id: str,
        text: str,
        msg_id: str | None,
        msg_seq: int | None,
        markdown: bool,
    ) -> Mapping[str, Any]: ...


def build_native_transport(
    app_id: str, secret: str, sandbox: bool, connect_timeout: float
) -> QqTransport:
    """默认传输层：`qq_transport.QqGatewayTransport`（只做装配，不掺渠道语义）。"""
    return QqGatewayTransport(
        app_id=app_id, secret=secret, sandbox=sandbox, connect_timeout=connect_timeout
    )


# ==============================================================================
# 适配器
# ==============================================================================


def passive_reply_rule(conversation_type: str) -> tuple[float, int]:
    """按会话场景取被动回复规则：`(窗口秒数, 允许条数)`。"""
    return PASSIVE_REPLY_RULES.get(conversation_type, PASSIVE_REPLY_FALLBACK_RULE)


@dataclass(slots=True)
class ReplyContext:
    """一条路由最近一条入站消息的回复上下文。

    `target_id` 与路由里的 `platform_user_id` 有意分开：群聊里前者是出站地址
    （group_openid），后者是提问的成员；窗口与条数也随会话类型不同，故一并记在上下文里。
    """

    msg_id: str
    target_id: str
    received_at: datetime
    window_seconds: float = PASSIVE_REPLY_FALLBACK_RULE[0]
    limit: int = PASSIVE_REPLY_FALLBACK_RULE[1]
    passive_used: int = 0
    next_seq: int = 1

    def passive_allowed(self, now: datetime) -> bool:
        if self.passive_used >= self.limit:
            return False
        # 只按"太旧"判失效：时间戳略超前于服务端时钟不该被当成过期，否则白用主动消息额度
        age = (now - self.received_at).total_seconds()
        return age <= self.window_seconds

    def take_seq(self) -> int:
        seq = self.next_seq
        self.next_seq += 1
        return seq


@dataclass(frozen=True, slots=True)
class _Delivery:
    target_id: str
    msg_id: str | None
    msg_seq: int | None


class QqBotAdapter(BaseChannelAdapter):
    """QQ 机器人适配器（能力：补发终稿；单条 4500 字符；一次回复最多 4 条）。"""

    channel = "qq_bot"
    adapter_version = ADAPTER_VERSION

    display_name = "QQ 机器人"
    #: 非密字段进实例 config，其余按密文处理；`owner_user_openid` 只由扫码路径写入。
    config_fields = ("app_id", "sandbox", "use_markdown", "owner_user_openid")
    mask_fields = ("app_id",)
    required_credentials = ("app_id", "secret")
    credential_fields = (
        CredentialField(name="app_id", label="机器人 AppID", secret=False),
        CredentialField(name="secret", label="机器人 AppSecret"),
        CredentialField(
            name="use_markdown",
            label="使用 markdown 消息（需平台已开通，留空为纯文本）",
            secret=False,
            required=False,
        ),
        CredentialField(
            name="sandbox",
            label="沙箱环境（填 true 使用沙箱域名）",
            secret=False,
            required=False,
        ),
    )

    def __init__(
        self,
        *,
        instance_id: str = "",
        config: Mapping[str, Any] | None = None,
        credentials: Mapping[str, str] | None = None,
        transport_factory: Callable[[str, str], QqTransport] | None = None,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
        max_text_length: int | None = None,
    ) -> None:
        super().__init__(
            instance_id=instance_id, config=config, credentials=credentials
        )
        self._transport_factory: Callable[[str, str], QqTransport] = (
            transport_factory or self._default_transport_factory
        )
        self._connect_timeout = connect_timeout_seconds
        self._max_text_length = (
            max_text_length
            or _config_int(self._config.get("max_text_length"))
            or MAX_TEXT_LENGTH
        )
        self._use_markdown = _config_bool(self._config.get("use_markdown"))
        self._transport: QqTransport | None = None
        self._contexts: dict[tuple[str, str, str], ReplyContext] = {}
        self._inflight_emits: set[asyncio.Task[None]] = set()
        self._accepting = False

    def _default_transport_factory(self, app_id: str, secret: str) -> QqTransport:
        return build_native_transport(
            app_id,
            secret,
            _config_bool(self._config.get("sandbox")),
            self._connect_timeout,
        )

    # ==========================================================================
    # 生命周期
    # ==========================================================================

    async def _connect(self) -> None:
        # 单连接不变式：同一个适配器只保留一条长连接（QQ 侧多连接会互相顶号）
        if self._transport is not None:
            await self._disconnect()
        transport = self._transport_factory(
            str(self._config.get("app_id") or ""),
            str(self._credentials.get("secret") or ""),
        )
        transport.on_event(self._handle_event)
        await transport.connect()
        self._transport = transport
        self._accepting = True
        logger.info(
            "QQ adapter connected: instance_id=%s app_id=%s",
            self.instance_id,
            self._masked_app_id(),
        )

    async def _disconnect(self) -> None:
        self._accepting = False
        transport, self._transport = self._transport, None
        for task in list(self._inflight_emits):
            task.cancel()
        self._inflight_emits.clear()
        if transport is not None:
            await transport.close()

    def is_alive(self) -> bool:
        """长连接健康度：接收循环退出（断开、鉴权失败、会话失效）即不健康。

        与企微同模型：不在传输层内部重连，断开即宣告不健康，由渠道网关的监督循环按退避重建
        （理由见 `QqGatewayTransport` 的 docstring）。
        """
        return (
            self._started and self._transport is not None and self._transport.is_alive()
        )

    async def _probe_capabilities(self) -> ChannelCapabilities:
        """QQ 没有"原地编辑消息"的接口：呈现方式固定为"补发新消息"。"""
        return ChannelCapabilities(
            can_edit_message=False,
            max_text_length=self._max_text_length,
            max_reply_segments=MAX_REPLY_SEGMENTS,
        )

    # ==========================================================================
    # 入站
    # ==========================================================================

    def _handle_event(self, frame: Mapping[str, Any]) -> None:
        if not self._accepting:
            return
        message = normalize_event(frame, instance_id=self.instance_id)
        if message is None:
            logger.debug(
                "QQ event ignored: instance_id=%s event=%s",
                self.instance_id,
                frame.get("t"),
            )
            return
        target_id = outbound_target(frame)
        if not target_id:
            # 群聊出站必须用 `group_openid`，而 `route.platform_user_id` 是成员的
            # member_openid——拿它去发会变成"私聊发给这个成员"。没有出站地址就
            # **宁可这一条不回**，也不退回提问者的 openid。
            logger.warning(
                "QQ message without an outbound target dropped: instance_id=%s "
                "event_id=%s event=%s",
                self.instance_id,
                message.platform_event_id,
                frame.get("t"),
            )
            return
        self._remember(message, target_id=target_id)
        self._track(asyncio.create_task(self.emit_inbound(message)))

    def _remember(self, message: InboundMessage, *, target_id: str) -> None:
        """记住这条消息的 `msg_id` 与投递目标，供随后的出站选择被动/主动。"""
        now = datetime.now(UTC)
        self._prune_contexts(now)
        existing = self._contexts.get(message.route.key)
        if existing is not None and existing.msg_id == message.platform_event_id:
            # 平台会重复推送同一条 `msg_id`（官方文档要求结合 `msg_seq` 去重）。**绝不能
            # 重置计数与 msg_seq**：序号从头来会被平台按 `msg_id + msg_seq` 判重复而失败。
            return
        window, limit = passive_reply_rule(message.route.conversation_type)
        self._contexts[message.route.key] = ReplyContext(
            msg_id=message.platform_event_id,
            target_id=target_id,
            received_at=message.received_at or now,
            window_seconds=window,
            limit=limit,
        )

    def _prune_contexts(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=CONTEXT_RETENTION_SECONDS)
        for key, context in list(self._contexts.items()):
            if context.received_at < cutoff:
                self._contexts.pop(key, None)
        while len(self._contexts) > MAX_TRACKED_CONTEXTS:
            oldest = min(self._contexts.items(), key=lambda item: item[1].received_at)
            self._contexts.pop(oldest[0], None)

    # ==========================================================================
    # 出站
    # ==========================================================================

    async def send_text(self, route: Route, text: str) -> DeliveryResult:
        delivery = self._plan_delivery(route)
        try:
            response = await self._require_transport().send_message(
                conversation_type=route.conversation_type,
                target_id=delivery.target_id,
                text=text,
                msg_id=delivery.msg_id,
                msg_seq=delivery.msg_seq,
                markdown=self._use_markdown,
            )
        except Exception as exc:
            result = self.classify_exception(exc)
            # 失败的这一次同样要记账：分界不是"成功/失败"，而是"平台是否可能已经计入"。
            # `uncertain` 证明不了没送到，少扣一次就会撞上"同一条入站消息最多 4 条"。
            self._consume_passive(route, delivery, consumed=not result.rejected)
            return result
        self._consume_passive(route, delivery, consumed=True)
        # 传输层契约是「没抛异常即已受理」，但非对象返回仍要挡一下，不能静默当成成功。
        if not isinstance(response, Mapping):
            logger.debug(
                "QQ transport returned a non-mapping response: instance_id=%s type=%s",
                self.instance_id,
                type(response).__name__,
            )
        reference = response.get("id") if isinstance(response, Mapping) else None
        return DeliveryResult.delivered_with(
            str(reference) if reference else (delivery.msg_id or "")
        )

    def _consume_passive(
        self, route: Route, delivery: _Delivery, *, consumed: bool
    ) -> None:
        """扣减被动额度：平台口径是"同一条入站消息下的所有出站共享上限"，占位消息也算一条。

        只有被动回复（带 `msg_id`）占额度；`consumed` 由调用方按三态给出，见 `send_text`。
        """
        if not consumed or delivery.msg_id is None:
            return
        context = self._contexts.get(route.key)
        if context is not None:
            context.passive_used += 1

    def _plan_delivery(self, route: Route) -> _Delivery:
        """决定这一条走被动回复还是主动消息（本地即可判定的确定性降级）。"""
        now = datetime.now(UTC)
        self._prune_contexts(now)
        context = self._contexts.get(route.key)
        if context is None:
            return _Delivery(
                target_id=route.platform_user_id, msg_id=None, msg_seq=None
            )
        if not context.passive_allowed(now):
            return _Delivery(target_id=context.target_id, msg_id=None, msg_seq=None)
        return _Delivery(
            target_id=context.target_id,
            msg_id=context.msg_id,
            msg_seq=context.take_seq(),
        )

    def _require_transport(self) -> QqTransport:
        transport = self._transport
        if transport is None:
            raise ConnectionError("the QQ adapter is not connected")
        return transport

    # ==========================================================================
    # 三态归类
    # ==========================================================================

    def classify_exception(self, exc: BaseException) -> DeliveryResult:
        """异常映射表（"代价不对称"纪律）。

        传输层已把异常归一化成"错误码 + 三态"，这里只搬运；**未知异常一律 `uncertain`**：无法
        确定是否送达时不重发，代价远小于重复投递。
        """
        if isinstance(exc, QqTransportError):
            return DeliveryResult(certainty=exc.certainty, error_code=exc.error_code)
        return DeliveryResult.uncertain_with(err.CHANNEL_DELIVERY_UNCERTAIN)

    # ==========================================================================
    # 测试与排障辅助
    # ==========================================================================

    def _masked_app_id(self) -> str:
        return BaseChannelAdapter.mask_credential(str(self._config.get("app_id") or ""))

    def _track(self, task: asyncio.Task[None]) -> None:
        self._inflight_emits.add(task)
        task.add_done_callback(self._inflight_emits.discard)

    async def wait_for_inbound(self) -> None:
        """等待在途的入站分发结束（测试与排障的公共入口）。

        入站帧在 `_handle_event` 里只做同步归一化，真正的分发是 `create_task`；需要确定性等待
        的调用方用这个入口，而不是去摸内部的在途任务集合。
        """
        while self._inflight_emits:
            settled = await asyncio.gather(
                *list(self._inflight_emits), return_exceptions=True
            )
            for item in settled:
                if isinstance(item, BaseException):
                    logger.warning(
                        "QQ inbound handler failed: instance_id=%s err=%r",
                        self.instance_id,
                        item,
                    )


def _config_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _config_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


# ==============================================================================
# 扫码接入驱动（官方 q.qq.com「lite 绑定任务」）
# ==============================================================================

#: 扫码服务（绑定任务）的地址。官方连接器分 production/test 两台主机，本项目只用
#: production：二维码页面永远在 production 域渲染，暴露成开关只会多一个配错点。
QR_SERVICE_BASE = "https://q.qq.com"
#: 申请绑定任务 / 轮询绑定结果
QR_CREATE_TASK_PATH = "/lite/create_bind_task"
QR_POLL_TASK_PATH = "/lite/poll_bind_result"
#: 二维码页面的路径（连接器构造：`?task_id=..&source=..&_wv=2`）
QR_CONNECT_PATH = "/qqbot/openclaw/connect.html"
#: 轮询间隔（官方连接器固定 2s；平台侧没有给建议值）
QR_POLL_INTERVAL_MS = 2000
#: 本地二维码有效期：界面倒计时与本次尝试的本地上限；平台何时作废由 `poll` 的 status
#: 决定，本值必须**不小于**平台真实时效（官方连接器取 5 分钟，此处同款）。
QR_TTL_SECONDS = 5 * 60
#: 一次接入尝试内最多换几次二维码（12 次 ≈ 1 小时），避免被遗弃的对话框无限申请任务
MAX_QR_REFRESHES = 12
#: 上报给平台的调用方标识（会出现在二维码页面上，留空则显示为"第三方机器人"）
DEFAULT_QR_SOURCE = "witty-service"

#: 绑定任务状态（官方连接器的 `BindStatus` 枚举）
BIND_STATUS_NONE = 0
BIND_STATUS_PENDING = 1
BIND_STATUS_COMPLETED = 2
BIND_STATUS_EXPIRED = 3

#: AES-256-GCM：key 32 字节；密文布局是 IV(12) || ciphertext || tag(16)
AES_GCM_KEY_BYTES = 32
AES_GCM_IV_BYTES = 12
AES_GCM_TAG_BYTES = 16

#: 注入式 POST 实现。约定：**可重试**的失败（超时、连接被重置）抛 `QqTransientError`，
#: 平台已表态的失败抛 `QqProvisioningError`；`_default_http_post` 已按此分类。
HttpPost = Callable[[str, Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


class QqProvisioningError(Exception):
    """扫码服务返回的**确定性**错误：平台已明确表态，重试也不会变。

    典型：retcode 非 0、返回体缺少必需字段、状态文件损坏——一律让本次接入尝试失败。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class QqTransientError(QqProvisioningError):
    """扫码服务的**瞬时**错误：请求可能压根没到平台（超时、连接被重置）。

    继承 `QqProvisioningError` 是让"忘了分类"的调用点仍走失败路径；但 `poll` /
    `_refreshed` **必须**在捕获基类之前先捕获它，把这次轮询当成"还没有结论"继续等——
    否则一次网络抖动就会把尝试写成 `failed`，而平台上的二维码其实还有效。
    """


def generate_bind_key() -> str:
    """绑定任务的 `key`：32 字节随机数的 base64，也是解密 AppSecret 的 AES-256 密钥，
    **只随 state 落在服务端**，不进任何响应。
    """
    return base64.b64encode(secrets.token_bytes(AES_GCM_KEY_BYTES)).decode("ascii")


def build_connect_url(task_id: str, source: str = DEFAULT_QR_SOURCE) -> str:
    """二维码内容：用户可以扫的授权页面地址（前端把它渲染成二维码图片）。"""
    query = urlencode({"task_id": task_id, "source": source, "_wv": "2"})
    return f"{QR_SERVICE_BASE}{QR_CONNECT_PATH}?{query}"


def decrypt_bind_secret(encrypted: str, key: str) -> str:
    """用绑定任务的 `key` 解出 AppSecret（AES-256-GCM）。

    官方连接器 `decryptSecret`：`key = base64decode(key)`（32 字节 -> AES-256），
    `blob = base64decode(bot_encrypt_secret)`，`iv = blob[:12]`、`tag = blob[-16:]`、
    `ct = blob[12:-16]`。明文布局因此是 **IV(12) || 密文 || tag(16)**（不是 Node 默认的
    iv||tag||ct）；`AESGCM.decrypt` 要"密文||tag"，故这里把 tag 挪到末尾。
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key_bytes = base64.b64decode(key, validate=True)
    blob = base64.b64decode(encrypted, validate=True)
    if len(key_bytes) != AES_GCM_KEY_BYTES:
        raise ValueError("the bind key is not 32 bytes")
    if len(blob) <= AES_GCM_IV_BYTES + AES_GCM_TAG_BYTES:
        raise ValueError("the encrypted secret is too short")
    iv = blob[:AES_GCM_IV_BYTES]
    tag = blob[-AES_GCM_TAG_BYTES:]
    ciphertext = blob[AES_GCM_IV_BYTES:-AES_GCM_TAG_BYTES]
    return AESGCM(key_bytes).decrypt(iv, ciphertext + tag, None).decode("utf-8")


async def _default_http_post(url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
    """默认的 POST 实现（`httpx` 已在主依赖中）。"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            response = await client.post(
                url, json=dict(payload), headers={"accept": "application/json"}
            )
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        # 服务端明确回了状态码：平台已表态，重试没有意义
        raise QqProvisioningError(
            "HTTP_STATUS", f"qr service returned {exc.response.status_code}"
        ) from exc
    except httpx.HTTPError as exc:
        # 其余 httpx 异常都是"没能拿到答复"，请求可能未到平台，属于可重试
        raise QqTransientError("HTTP_ERROR", f"qr request failed: {exc}") from exc
    except ValueError as exc:
        raise QqProvisioningError(
            "BAD_RESPONSE", "qr response is not valid JSON"
        ) from exc
    if not isinstance(data, Mapping):
        raise QqProvisioningError("BAD_RESPONSE", "qr response is not an object")
    return data


class QqProvisioningDriver:
    """QQ 扫码接入驱动：官方「lite 绑定任务」流程。

        POST {base}/lite/create_bind_task   {"key": "<32 随机字节的 base64>"} -> data.task_id
        二维码内容 = {base}/qqbot/openclaw/connect.html?task_id=..&source=..&_wv=2
        POST {base}/lite/poll_bind_result   {"task_id": ..} -> data.status（0 无 / 1 待扫 /
            2 完成 / 3 过期）+ bot_appid + bot_encrypt_secret + user_openid

    两个必须自己补上的点：平台判过期后旧 `task_id` 作废，`poll` 会立刻换新任务并把新二维码
    + 新 state 交回编排层（用户侧表现为"二维码自动刷新"）；平台只回 `bot_encrypt_secret`，
    必须用本次任务的 `key` 做 AES-256-GCM 解密（见 `decrypt_bind_secret`）。

    `state` 形如 `{"task_id": .., "key": .., "refreshes": n}`：平台侧临时凭据，由编排层
    加密落在 0600 文件里，**不出现在任何响应里**；`http_post` 可注入，测试无需联网。
    """

    channel: ClassVar[str] = "qq_bot"

    def __init__(
        self,
        *,
        api_base: str = QR_SERVICE_BASE,
        http_post: HttpPost | None = None,
        clock: Callable[[], datetime] | None = None,
        source: str = DEFAULT_QR_SOURCE,
        task_ttl_seconds: float = QR_TTL_SECONDS,
        max_refreshes: int = MAX_QR_REFRESHES,
        poll_interval_ms: int = QR_POLL_INTERVAL_MS,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._http_post = http_post or _default_http_post
        self._clock = clock or (lambda: datetime.now(UTC))
        self._source = source
        self._task_ttl_seconds = task_ttl_seconds
        self._max_refreshes = max(0, max_refreshes)
        self._poll_interval_ms = poll_interval_ms

    async def begin(self) -> ProvisioningSession:
        key = generate_bind_key()
        task_id = await self._create_task(key)
        return ProvisioningSession(
            qr_content=build_connect_url(task_id, self._source),
            expires_at=self._clock() + timedelta(seconds=self._task_ttl_seconds),
            poll_interval_ms=self._poll_interval_ms,
            state=_encode_state(task_id=task_id, key=key, refreshes=0),
        )

    async def poll(self, state: bytes) -> ProvisioningOutcome:
        parsed = _decode_state(state)
        if parsed is None:
            return ProvisioningOutcome(status=STATUS_FAILED, error_code="QQ_BAD_STATE")
        task_id, key, refreshes = parsed
        try:
            payload = await self._post(QR_POLL_TASK_PATH, {"task_id": task_id})
        except QqTransientError:
            # 网络抖动只是**这次轮询**没有结论：保持 waiting，客户端按 poll_interval 再来。
            logger.warning(
                "QQ bind poll failed transiently; staying in waiting: task_id=%s",
                task_id,
                exc_info=True,
            )
            return ProvisioningOutcome(status=STATUS_WAITING)
        except QqProvisioningError as exc:
            return ProvisioningOutcome(status=STATUS_FAILED, error_code=exc.code)
        retcode = payload.get("retcode")
        if retcode not in (None, 0, "0"):
            return ProvisioningOutcome(
                status=STATUS_FAILED,
                error_code=f"QQ_{retcode}",
            )
        data = _mapping_of(payload.get("data"))
        status = _bind_status(data.get("status"))
        if status == BIND_STATUS_COMPLETED:
            return self._completed(data, key)
        if status == BIND_STATUS_EXPIRED:
            return await self._refreshed(refreshes)
        if status in (BIND_STATUS_NONE, BIND_STATUS_PENDING):
            return ProvisioningOutcome(status=STATUS_WAITING)
        return ProvisioningOutcome(status=STATUS_FAILED, error_code="QQ_BAD_STATUS")

    # ------------------------------------------------------------------ 内部

    def _completed(self, data: Mapping[str, Any], key: str) -> ProvisioningOutcome:
        """扫码完成：解出 AppSecret（并顺手记下扫码人）。"""
        app_id = _first_str(data, ("bot_appid", "bot_app_id", "app_id"))
        encrypted = _first_str(data, ("bot_encrypt_secret", "encrypt_secret"))
        if not app_id or not encrypted:
            return ProvisioningOutcome(
                status=STATUS_FAILED, error_code="QQ_MISSING_CREDENTIALS"
            )
        try:
            secret = decrypt_bind_secret(encrypted, key)
        except Exception:
            # 解不开就如实上报"凭据拿不到"，不落一个空密码进库
            logger.warning(
                "QQ bind result could not be decrypted: app_id=%s",
                BaseChannelAdapter.mask_credential(app_id),
                exc_info=True,
            )
            return ProvisioningOutcome(status=STATUS_FAILED, error_code="QQ_BAD_SECRET")
        credentials = {"app_id": app_id, "secret": secret}
        owner = _first_str(data, ("user_openid", "user_open_id"))
        if owner:
            # 扫码人 openid：四渠道中唯一能拿到的"所有者身份"，落成非密配置
            credentials["owner_user_openid"] = owner
        return ProvisioningOutcome(status=STATUS_SUCCEEDED, credentials=credentials)

    async def _refreshed(self, refreshes: int) -> ProvisioningOutcome:
        """二维码过期：换一个新任务，让同一个接入尝试继续下去。"""
        if refreshes >= self._max_refreshes:
            # 刷新次数用尽：如实报"过期"，让用户重新点接入，而不是无限申请任务
            return ProvisioningOutcome(status=STATUS_EXPIRED)
        key = generate_bind_key()
        try:
            task_id = await self._create_task(key)
        except QqTransientError:
            # 换任务时网络抖动：旧任务已被平台作废，但这次尝试仍是 waiting——下次轮询
            # 会再判一次过期、再换一次，等于自动重试。
            logger.warning(
                "QQ bind refresh failed transiently; retrying on the next poll",
                exc_info=True,
            )
            return ProvisioningOutcome(status=STATUS_WAITING)
        except QqProvisioningError as exc:
            return ProvisioningOutcome(status=STATUS_FAILED, error_code=exc.code)
        return ProvisioningOutcome(
            status=STATUS_WAITING,
            qr_content=build_connect_url(task_id, self._source),
            expires_at=self._clock() + timedelta(seconds=self._task_ttl_seconds),
            state=_encode_state(task_id=task_id, key=key, refreshes=refreshes + 1),
        )

    async def _create_task(self, key: str) -> str:
        payload = await self._post(QR_CREATE_TASK_PATH, {"key": key})
        retcode = payload.get("retcode")
        if retcode not in (None, 0, "0"):
            raise QqProvisioningError(
                f"QQ_{retcode}", str(payload.get("msg") or "create_bind_task failed")
            )
        task_id = _first_str(_mapping_of(payload.get("data")), ("task_id",))
        if not task_id:
            raise QqProvisioningError(
                "QQ_NO_TASK", "create_bind_task did not return a task_id"
            )
        return task_id

    async def _post(self, path: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return await self._http_post(f"{self._api_base}{path}", payload)


def _encode_state(*, task_id: str, key: str, refreshes: int) -> bytes:
    return json.dumps({"task_id": task_id, "key": key, "refreshes": refreshes}).encode(
        "utf-8"
    )


def _decode_state(state: bytes) -> tuple[str, str, int] | None:
    """解出 `(task_id, key, refreshes)`；状态损坏返回 None（当次轮询按失败处理）。"""
    try:
        parsed = json.loads(state.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    task_id = _first_str(parsed, ("task_id",))
    key = _first_str(parsed, ("key",))
    if not task_id or not key:
        return None
    refreshes = parsed.get("refreshes")
    return task_id, key, refreshes if isinstance(refreshes, int) else 0


def _mapping_of(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _bind_status(value: object) -> int | None:
    """绑定任务状态：平台给的是数字，这里也接受数字字符串。

    官方连接器只认数字（`=== 2`）。此处宽松是有意的：把 `"2"`（已扫上）当成未知状态
    会让一次本该成功的接入失败，而多认一种写法不会带来错误结果。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


register_adapter(QqBotAdapter)
register_driver(QqProvisioningDriver)

__all__ = [
    "ADAPTER_VERSION",
    "AES_GCM_IV_BYTES",
    "AES_GCM_KEY_BYTES",
    "AES_GCM_TAG_BYTES",
    "BIND_STATUS_COMPLETED",
    "BIND_STATUS_EXPIRED",
    "BIND_STATUS_NONE",
    "BIND_STATUS_PENDING",
    "CONTEXT_RETENTION_SECONDS",
    "DEFAULT_CONNECT_TIMEOUT_SECONDS",
    "DEFAULT_QR_SOURCE",
    "EVENT_C2C_MESSAGE_CREATE",
    "EVENT_GROUP_AT_MESSAGE_CREATE",
    "MAX_QR_REFRESHES",
    "MAX_REPLY_SEGMENTS",
    "MAX_TEXT_LENGTH",
    "MAX_TRACKED_CONTEXTS",
    "MSG_TYPE_MARKDOWN",
    "MSG_TYPE_TEXT",
    "PASSIVE_REPLY_FALLBACK_RULE",
    "PASSIVE_REPLY_RULES",
    "QR_CONNECT_PATH",
    "QR_CREATE_TASK_PATH",
    "QR_POLL_INTERVAL_MS",
    "QR_POLL_TASK_PATH",
    "QR_SERVICE_BASE",
    "QR_TTL_SECONDS",
    "QqBotAdapter",
    "QqProvisioningDriver",
    "QqProvisioningError",
    "QqTransientError",
    "QqTransport",
    "QqTransportError",
    "ReplyContext",
    "build_connect_url",
    "build_native_transport",
    "decrypt_bind_secret",
    "generate_bind_key",
    "normalize_event",
    "outbound_target",
    "passive_reply_rule",
]
