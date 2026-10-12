"""统一 ``stream.error`` / ``client.error`` 载荷契约。

所有 runtime 产出的错误事件 payload 必须形如：

    {
        "code": "UPSTREAM_AUTH",              # 类别枚举（SCREAMING_SNAKE）
        "message": "Invalid Authentication",  # 一句话根因（单行、截断）
        "source": "upstream",                 # upstream | transport | runtime | internal
        "retryable": False,
        "detail": {                           # best-effort，无有效键则整体省略
            "provider_code": "APIError",      # 上游原始 code/name，原样保留
            "status_code": 401,
            "url": "...",
            "response_body": "...(截断)",
            "request_id": "...",
        },
    }

类别码按"错误来源"分类而非按 adaptor 分类：新接 adaptor 复用同一词表，
消费端（agent_manager 日志 / scheduled_task / channels 文案 / 前端）按类别
统一分支，上游原码经 ``detail.provider_code`` 保留排障信息。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

# 载荷字段截断上限：message / response_body 落日志与 DB，防止上游巨包刷屏。
_MESSAGE_MAX_CHARS = 500
_RESPONSE_BODY_MAX_CHARS = 500

# openclaw lifecycle error 的 message 形如 "HTTP 401: Invalid Authentication"，
# 前缀解析出状态码用于分类，剩余部分作为根因 message。
_HTTP_PREFIX_RE = re.compile(r"^HTTP\s+(\d{3})\b[:\s]*(.*)$", re.DOTALL)

# 配额耗尽特征：部分供应商以 429 + "insufficient quota / exceeded your
# current quota / insufficient balance / billing" 表达**永久性**配额/余额
# 耗尽——与可重试的瞬时限流语义相反，须按 message 关键字甄别。
_QUOTA_HINT_RE = re.compile(
    r"insufficient[ _-]*(quota|balance)|quota[ _-]*(exceed|exhaust)"
    r"|exceed(?:ed)?[ _-]*your[ _-]*current[ _-]*quota|billing",
    re.IGNORECASE,
)


class StreamErrorCode(StrEnum):
    """跨 adaptor 的错误类别枚举。"""

    UPSTREAM_AUTH = "UPSTREAM_AUTH"
    UPSTREAM_RATE_LIMIT = "UPSTREAM_RATE_LIMIT"
    UPSTREAM_QUOTA_EXCEEDED = "UPSTREAM_QUOTA_EXCEEDED"
    UPSTREAM_REQUEST = "UPSTREAM_REQUEST"
    UPSTREAM_SERVER = "UPSTREAM_SERVER"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    TRANSPORT_CLOSED = "TRANSPORT_CLOSED"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    RUNTIME_PROTOCOL_ERROR = "RUNTIME_PROTOCOL_ERROR"
    RUNTIME_LIFECYCLE_ERROR = "RUNTIME_LIFECYCLE_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class StreamErrorSource(StrEnum):
    """错误来源枚举：与类别码前缀一一对应（UPSTREAM_/TRANSPORT_/RUNTIME_），
    其余归 internal。"""

    UPSTREAM = "upstream"
    TRANSPORT = "transport"
    RUNTIME = "runtime"
    INTERNAL = "internal"


def classify_from_status(status_code: int | None) -> StreamErrorCode | None:
    """HTTP 状态码 → 类别；无法分类（None/1xx/2xx/3xx）返回 None。

    504 Gateway Timeout 单独归 ``UPSTREAM_TIMEOUT``：本质是上游响应超时
    （代理链路最常见），与 502/503 的"服务端故障"在重试节奏和用户文案上
    都应区分。
    """
    if not isinstance(status_code, int) or isinstance(status_code, bool):
        return None
    if status_code in (401, 403):
        return StreamErrorCode.UPSTREAM_AUTH
    if status_code == 429:
        return StreamErrorCode.UPSTREAM_RATE_LIMIT
    if status_code in (408, 504):
        return StreamErrorCode.UPSTREAM_TIMEOUT
    if 400 <= status_code < 500:
        return StreamErrorCode.UPSTREAM_REQUEST
    if 500 <= status_code < 600:
        return StreamErrorCode.UPSTREAM_SERVER
    return None


def classify_dsh_reason(reason: str | None) -> StreamErrorCode:
    """DshClientError.reason slug → 类别（slug 契约见 dsh_client._map_error_reason）。

    ``config-invalid``（provider 路由/凭据配置错误）与 ``not-started``
    （harness 未启动即调用）是永久性错误，必须显式映射，不得落入默认的
    可重试 TRANSPORT_ERROR。
    """
    if reason == "transport-closed":
        return StreamErrorCode.TRANSPORT_CLOSED
    if reason in ("json-rpc-error", "harness-error"):
        return StreamErrorCode.TRANSPORT_ERROR
    if reason == "timeout":
        return StreamErrorCode.UPSTREAM_TIMEOUT
    if reason == "protocol-error":
        return StreamErrorCode.RUNTIME_PROTOCOL_ERROR
    if reason == "config-invalid":
        # provider 凭据/路由配置问题：请求侧永久错误，重试无意义。
        return StreamErrorCode.UPSTREAM_REQUEST
    if reason == "not-started":
        return StreamErrorCode.RUNTIME_LIFECYCLE_ERROR
    return StreamErrorCode.TRANSPORT_ERROR


def classify_from_code_hint(code: str | None) -> StreamErrorCode:
    """上游原始 code 字符串 → 类别（关键字匹配，兜底 UPSTREAM_REQUEST）。

    dsh turn-end 的 ``reason.error.code`` 可能是任意上游直传码
    （"AUTH" / "INVALID_REQUEST" / "MODEL_NOT_FOUND" ...），仅作 hint。
    """
    upper = code.upper() if isinstance(code, str) else ""
    if not upper:
        return StreamErrorCode.UPSTREAM_REQUEST
    if "AUTH" in upper:
        return StreamErrorCode.UPSTREAM_AUTH
    if "RATE" in upper or "QUOTA" in upper:
        return StreamErrorCode.UPSTREAM_RATE_LIMIT
    if "TIMEOUT" in upper or "DEADLINE" in upper:
        return StreamErrorCode.UPSTREAM_TIMEOUT
    if "INVALID" in upper or "BAD_REQUEST" in upper or "NOT_FOUND" in upper:
        return StreamErrorCode.UPSTREAM_REQUEST
    if "INTERNAL" in upper or "SERVER" in upper or "UNAVAILABLE" in upper:
        return StreamErrorCode.UPSTREAM_SERVER
    return StreamErrorCode.UPSTREAM_REQUEST


def _apply_quota_override(
    code: StreamErrorCode, message: str, status_code: int | None = None
) -> StreamErrorCode:
    """配额/余额耗尽 → ``UPSTREAM_QUOTA_EXCEEDED``。均须 message 关键字佐证。

    两个入口：

    - 有状态码且为 402（DeepSeek 等以 402 表达余额不足，初始分类 REQUEST）
      或 429（部分供应商以 429 表达永久性配额耗尽，初始分类 RATE_LIMIT）；
    - 无状态码且初始分类为 RUNTIME_LIFECYCLE_ERROR（openclaw 自由文本）
      或 UPSTREAM_RATE_LIMIT（dsh turn-end 只给 ``code="QUOTA_EXCEEDED"``
      之类的 hint、无 status，初始分类只能命中 RATE_LIMIT）：状态分类
      不可用，均须按 message 关键字改判，否则配额耗尽会被当成可重试
      限流反复重试。

    其他组合不改判，避免把鉴权失败等含 "billing" 字样的错误误伤。改判后
    天然不可重试（不在 _is_retryable_code 集合中）。
    """
    if status_code is not None:
        allowed = status_code in (402, 429) and code in (
            StreamErrorCode.UPSTREAM_RATE_LIMIT,
            StreamErrorCode.UPSTREAM_REQUEST,
        )
    else:
        allowed = code in (
            StreamErrorCode.RUNTIME_LIFECYCLE_ERROR,
            StreamErrorCode.UPSTREAM_RATE_LIMIT,
        )
    if not allowed:
        return code
    if isinstance(message, str) and _QUOTA_HINT_RE.search(message):
        return StreamErrorCode.UPSTREAM_QUOTA_EXCEEDED
    return code


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _single_line(text: str) -> str:
    """压成单行（日志一行一条），再截断。"""
    return _truncate(" ".join(text.split()), _MESSAGE_MAX_CHARS)


# 部分上游（实测 dsh harness）把整个 JSON 错误体塞进 message 文本，
# 形如 ``401: {"message":"Invalid Authentication","type":"..."}``。
# 契约要求 message 是"一句话人类可读根因"，在此统一拆包。
_EMBEDDED_ERROR_RE = re.compile(r"^\s*\d{3}\s*:\s*(\{.*\})\s*$", re.DOTALL)


def _extract_embedded_error_message(message: str) -> str:
    """拆包 ``NNN: {json}`` 形态的 message，取出内层 error.message / message。

    JSON 解析失败或无内层 message 时原样返回；仅接受 dict 形态，避免误伤
    恰好像 JSON 的普通文本。
    """
    match = _EMBEDDED_ERROR_RE.match(message)
    if match is None:
        return message
    try:
        body = json.loads(match.group(1))
    except ValueError:
        return message
    if not isinstance(body, dict):
        return message
    nested = body.get("error")
    for candidate in (nested if isinstance(nested, dict) else None, body):
        if candidate is None:
            continue
        inner = candidate.get("message")
        if isinstance(inner, str) and inner:
            return inner
    return message


def _clean_detail(detail: dict[str, Any] | None) -> dict[str, Any] | None:
    """剔除空值键；整体为空则返回 None（载荷省略 detail 键）。"""
    if not isinstance(detail, dict):
        return None
    cleaned = {key: value for key, value in detail.items() if value not in (None, "")}
    return cleaned or None


def normalize_stream_error(
    *,
    code: StreamErrorCode | str,
    message: str,
    source: StreamErrorSource | str,
    retryable: bool,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """组装统一错误载荷（全仓唯一出口）。

    ``source`` 接受枚举或字面量，字面量经 ``StreamErrorSource`` 枚举校验：
    笔误会立即抛 ValueError 而非静默产出脏载荷。
    ``code`` 接受枚举或字符串，字符串**原样放行、不做词表校验**——这是为
    ``client.error`` 的协议层私有码预留的口子（见 session_ws_router）
    """
    if not isinstance(code, StreamErrorCode):
        code = str(code)
    if not isinstance(source, StreamErrorSource):
        source = StreamErrorSource(source)
    payload: dict[str, Any] = {
        "code": code.value if isinstance(code, StreamErrorCode) else code,
        "message": (
            _single_line(_extract_embedded_error_message(message))
            if isinstance(message, str)
            else ""
        ),
        "source": source.value,
        "retryable": bool(retryable),
    }
    cleaned = _clean_detail(detail)
    if cleaned is not None:
        if isinstance(cleaned.get("response_body"), str):
            cleaned["response_body"] = _truncate(
                cleaned["response_body"], _RESPONSE_BODY_MAX_CHARS
            )
        payload["detail"] = cleaned
    return payload


def internal_error(message: str) -> dict[str, Any]:
    """便捷构造：内部错误载荷（witty-service 侧合成 stream.error 统一走此出口）。"""
    return normalize_stream_error(
        code=StreamErrorCode.INTERNAL_ERROR,
        message=message,
        source=StreamErrorSource.INTERNAL,
        retryable=False,
    )


def _is_retryable_code(code: StreamErrorCode) -> bool:
    return code in (
        StreamErrorCode.UPSTREAM_RATE_LIMIT,
        StreamErrorCode.UPSTREAM_SERVER,
        StreamErrorCode.UPSTREAM_TIMEOUT,
        StreamErrorCode.TRANSPORT_CLOSED,
        StreamErrorCode.TRANSPORT_ERROR,
    )


def _source_for_code(code: StreamErrorCode) -> StreamErrorSource:
    """类别码前缀 → source（UPSTREAM_/TRANSPORT_/RUNTIME_/其余 internal）"""
    if code.value.startswith("UPSTREAM_"):
        return StreamErrorSource.UPSTREAM
    if code.value.startswith("TRANSPORT_"):
        return StreamErrorSource.TRANSPORT
    if code.value.startswith("RUNTIME_"):
        return StreamErrorSource.RUNTIME
    return StreamErrorSource.INTERNAL


def _classify_upstream(
    status_code: int | None,
    message: str,
    *,
    code_hint: str | None = None,
    fallback: StreamErrorCode = StreamErrorCode.UPSTREAM_REQUEST,
) -> StreamErrorCode:
    """上游分类共用管道：状态码优先 → code hint → fallback，再做配额改判。

    opencode / openclaw / dsh turn-end / orchestrator 异常四条路径共用；
    新接 adaptor 只需写字段抽取部分，分类口径在此单点维护。
    """
    code = classify_from_status(status_code)
    if code is None:
        code = classify_from_code_hint(code_hint) if code_hint else fallback
    return _apply_quota_override(code, message, status_code)


def normalize_opencode_error(raw: Mapping[str, Any]) -> dict[str, Any]:
    """opencode ``session.error`` 原始事件 → 统一载荷。

    根因路径 ``raw.error.data.{message, statusCode, responseBody,
    metadata.url, isRetryable}``，``raw.error.name`` 为 provider 异常名
    （如 "APIError"）。缺 error 对象（question.asked 畸形兜底等）按协议
    错误处理，raw JSON 截断后放入 detail 保留诊断信息。
    """
    error = raw.get("error")
    if not isinstance(error, Mapping):
        return normalize_stream_error(
            code=StreamErrorCode.RUNTIME_PROTOCOL_ERROR,
            message="opencode emitted an event without a parsable error object",
            source="runtime",
            retryable=False,
            detail={"raw_event": _truncate(str(dict(raw)), _RESPONSE_BODY_MAX_CHARS)},
        )

    data = error.get("data")
    data = data if isinstance(data, Mapping) else {}
    message = data.get("message")
    message = (
        message if isinstance(message, str) and message else "opencode session error"
    )
    status_code = data.get("statusCode")
    status_code = status_code if isinstance(status_code, int) else None

    code = _classify_upstream(
        status_code, message, fallback=StreamErrorCode.RUNTIME_LIFECYCLE_ERROR
    )
    explicit_retryable = data.get("isRetryable")
    retryable = (
        bool(explicit_retryable)
        if isinstance(explicit_retryable, bool)
        else _is_retryable_code(code)
    )
    # 配额耗尽是永久性错误：即使上游误标 isRetryable=True 也不重试。
    if code == StreamErrorCode.UPSTREAM_QUOTA_EXCEEDED:
        retryable = False

    detail: dict[str, Any] = {"provider_code": error.get("name")}
    if status_code is not None:
        detail["status_code"] = status_code
    metadata = data.get("metadata")
    if isinstance(metadata, Mapping):
        url = metadata.get("url")
        if isinstance(url, str) and url:
            detail["url"] = url
    body = data.get("responseBody")
    if isinstance(body, str) and body:
        detail["response_body"] = body

    return normalize_stream_error(
        code=code,
        message=message,
        source="upstream",
        retryable=retryable,
        detail=detail,
    )


def normalize_openclaw_lifecycle_error(data: Mapping[str, Any]) -> dict[str, Any]:
    """openclaw lifecycle error data → 统一载荷。

    上游把 "HTTP 401: Invalid Authentication" 这类文本写进 ``message``
    （或 ``error``）；能解析出状态码则按状态分类，剩余文本作根因 message，
    否则按 lifecycle 错误兜底。``data.code`` 原样保留为 provider_code。
    """
    raw_message = data.get("message")
    if not isinstance(raw_message, str) or not raw_message:
        raw_message = data.get("error")
    raw_message = raw_message if isinstance(raw_message, str) and raw_message else ""

    provider_code = data.get("code")
    provider_code = (
        provider_code if isinstance(provider_code, str) and provider_code else None
    )

    match = _HTTP_PREFIX_RE.match(raw_message)
    if match is not None:
        status_code = int(match.group(1))
        root_cause = match.group(2).strip() or raw_message
        code = _classify_upstream(
            status_code, root_cause, fallback=StreamErrorCode.RUNTIME_LIFECYCLE_ERROR
        )
        return normalize_stream_error(
            code=code,
            message=root_cause,
            source="upstream",
            retryable=_is_retryable_code(code),
            detail={
                "provider_code": provider_code,
                "status_code": status_code,
            },
        )

    # 无 "HTTP NNN" 前缀的自由文本（openclaw 常态）：无法按状态分类，先按
    # lifecycle 兜底，再经配额关键字改判（如 "billing error / insufficient
    # balance" → UPSTREAM_QUOTA_EXCEEDED，与 opencode/dsh 口径对齐）。
    fallback_code = _apply_quota_override(
        StreamErrorCode.RUNTIME_LIFECYCLE_ERROR, raw_message
    )
    # 改判成功说明根因是上游供应商（message 直指 provider 计费），source
    # 相应从 runtime 升为 upstream；其余保持 lifecycle 语义。
    source = (
        "upstream"
        if fallback_code == StreamErrorCode.UPSTREAM_QUOTA_EXCEEDED
        else "runtime"
    )
    return normalize_stream_error(
        code=fallback_code,
        message=raw_message or "openclaw lifecycle stream error",
        source=source,
        retryable=False,
        detail={"provider_code": provider_code},
    )


def normalize_dsh_turn_error(error: Mapping[str, Any]) -> dict[str, Any]:
    """dsh turn-end ``reason.error`` → 统一载荷。

    实测样本：``{"code": "AUTH", "message": "...api key...invalid", "status": 401}``、
    ``{"code": "INVALID_REQUEST", "message": "model not found", "status": 400}``。
    优先按 status 分类，缺失时按 code hint。
    """
    code_str = error.get("code")
    code_str = code_str if isinstance(code_str, str) and code_str else None
    message = error.get("message")
    message = (
        message if isinstance(message, str) and message else "dsh turn ended with error"
    )
    status_code = error.get("status")
    status_code = status_code if isinstance(status_code, int) else None

    code = _classify_upstream(status_code, message, code_hint=code_str)
    return normalize_stream_error(
        code=code,
        message=message,
        source="upstream",
        retryable=_is_retryable_code(code),
        detail={"provider_code": code_str, "status_code": status_code},
    )


def normalize_dsh_client_error(reason: str, message: str) -> dict[str, Any]:
    """dsh 客户端异常（DshClientError）→ 统一载荷；reason 是 dsh_client 的 slug。

    source 随类别推导：传输类 slug → transport，配置/生命周期类 slug →
    upstream / runtime，不再一律标 transport。
    """
    code = classify_dsh_reason(reason)
    return normalize_stream_error(
        code=code,
        message=message,
        source=_source_for_code(code),
        retryable=_is_retryable_code(code),
        detail={"provider_code": reason},
    )


def normalize_exception_error(exc: BaseException) -> dict[str, Any]:
    """orchestrator 捕获的异常 → 统一载荷。

    优先级：``status_code`` 属性（含 ``response.status_code``，httpx
    ``HTTPStatusError`` 把状态码挂在 response 上）→ ``reason`` slug
    （DshClientError）→ 异常类型嗅探（httpx 传输/超时）→ INTERNAL_ERROR。
    """
    status_code = getattr(exc, "status_code", None)
    if not isinstance(status_code, int):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        message = str(exc) or "runtime request failed"
        code = _classify_upstream(
            status_code, message, fallback=StreamErrorCode.INTERNAL_ERROR
        )
        return normalize_stream_error(
            code=code,
            message=message,
            source=_source_for_code(code),
            retryable=_is_retryable_code(code),
            detail={
                "provider_code": getattr(exc, "code", None),
                "status_code": status_code,
            },
        )

    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        return normalize_dsh_client_error(reason, str(exc) or reason)

    # httpx 传输异常嗅探：模块名匹配避免硬依赖 httpx。
    module = type(exc).__module__ or ""
    if module.startswith("httpx"):
        if "timeout" in type(exc).__name__.lower():
            return normalize_stream_error(
                code=StreamErrorCode.UPSTREAM_TIMEOUT,
                message=str(exc) or "upstream request timed out",
                source=_source_for_code(StreamErrorCode.UPSTREAM_TIMEOUT),
                retryable=True,
            )
        return normalize_stream_error(
            code=StreamErrorCode.TRANSPORT_ERROR,
            message=str(exc) or "upstream transport error",
            source=_source_for_code(StreamErrorCode.TRANSPORT_ERROR),
            retryable=True,
        )

    provider_code = getattr(exc, "code", None)
    provider_code = (
        provider_code if isinstance(provider_code, str) and provider_code else None
    )
    return normalize_stream_error(
        code=StreamErrorCode.INTERNAL_ERROR,
        message=str(exc) or "runtime request failed",
        source="internal",
        retryable=False,
        detail={"provider_code": provider_code},
    )
