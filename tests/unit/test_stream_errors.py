"""stream_errors 归一化模块单测。

三个真实故障样本（配置错误模型后的 401）驱动分类与载荷金样断言。
"""

from __future__ import annotations

import pytest

from witty_agent_server.runtimes.stream_errors import (
    StreamErrorCode,
    StreamErrorSource,
    classify_dsh_reason,
    classify_from_code_hint,
    classify_from_status,
    internal_error,
    normalize_dsh_client_error,
    normalize_dsh_turn_error,
    normalize_exception_error,
    normalize_openclaw_lifecycle_error,
    normalize_opencode_error,
    normalize_stream_error,
)

# ==============================================================================
# 分类函数（表驱动）
# ==============================================================================


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (401, StreamErrorCode.UPSTREAM_AUTH),
        (403, StreamErrorCode.UPSTREAM_AUTH),
        (429, StreamErrorCode.UPSTREAM_RATE_LIMIT),
        (408, StreamErrorCode.UPSTREAM_TIMEOUT),
        # 504 Gateway Timeout 语义上是上游超时，与 502/503 的服务端故障区分
        (504, StreamErrorCode.UPSTREAM_TIMEOUT),
        (400, StreamErrorCode.UPSTREAM_REQUEST),
        (404, StreamErrorCode.UPSTREAM_REQUEST),
        (500, StreamErrorCode.UPSTREAM_SERVER),
        (503, StreamErrorCode.UPSTREAM_SERVER),
        (None, None),
        (200, None),
        (301, None),
    ],
)
def test_classify_from_status(status_code, expected):
    assert classify_from_status(status_code) is expected


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("transport-closed", StreamErrorCode.TRANSPORT_CLOSED),
        ("json-rpc-error", StreamErrorCode.TRANSPORT_ERROR),
        ("harness-error", StreamErrorCode.TRANSPORT_ERROR),
        ("timeout", StreamErrorCode.UPSTREAM_TIMEOUT),
        ("protocol-error", StreamErrorCode.RUNTIME_PROTOCOL_ERROR),
        # 永久性错误不得落入默认的可重试 TRANSPORT_ERROR
        ("config-invalid", StreamErrorCode.UPSTREAM_REQUEST),
        ("not-started", StreamErrorCode.RUNTIME_LIFECYCLE_ERROR),
        ("unknown-slug", StreamErrorCode.TRANSPORT_ERROR),
    ],
)
def test_classify_dsh_reason(reason, expected):
    assert classify_dsh_reason(reason) is expected


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("AUTH", StreamErrorCode.UPSTREAM_AUTH),
        ("invalid_authentication_error", StreamErrorCode.UPSTREAM_AUTH),
        ("RATE_LIMITED", StreamErrorCode.UPSTREAM_RATE_LIMIT),
        ("QUOTA_EXCEEDED", StreamErrorCode.UPSTREAM_RATE_LIMIT),
        ("REQUEST_TIMEOUT", StreamErrorCode.UPSTREAM_TIMEOUT),
        ("INVALID_REQUEST", StreamErrorCode.UPSTREAM_REQUEST),
        ("MODEL_NOT_FOUND", StreamErrorCode.UPSTREAM_REQUEST),
        ("INTERNAL_ERROR", StreamErrorCode.UPSTREAM_SERVER),
        ("SERVICE_UNAVAILABLE", StreamErrorCode.UPSTREAM_SERVER),
        ("", StreamErrorCode.UPSTREAM_REQUEST),
        (None, StreamErrorCode.UPSTREAM_REQUEST),
    ],
)
def test_classify_from_code_hint(code, expected):
    assert classify_from_code_hint(code) is expected


# ==============================================================================
# normalize_stream_error 基础行为
# ==============================================================================


def test_normalize_stream_error_golden_payload():
    payload = normalize_stream_error(
        code=StreamErrorCode.UPSTREAM_AUTH,
        message="Invalid Authentication",
        source="upstream",
        retryable=False,
        detail={
            "provider_code": "APIError",
            "status_code": 401,
            "url": "https://api.moonshot.cn/v1/chat/completions",
            "response_body": '{"error":{"message":"Invalid Authentication"}}',
        },
    )
    assert payload == {
        "code": "UPSTREAM_AUTH",
        "message": "Invalid Authentication",
        "source": "upstream",
        "retryable": False,
        "detail": {
            "provider_code": "APIError",
            "status_code": 401,
            "url": "https://api.moonshot.cn/v1/chat/completions",
            "response_body": '{"error":{"message":"Invalid Authentication"}}',
        },
    }


def test_normalize_stream_error_omits_empty_detail():
    payload = normalize_stream_error(
        code=StreamErrorCode.INTERNAL_ERROR,
        message="boom",
        source="internal",
        retryable=False,
        detail={"provider_code": None, "status_code": None},
    )
    assert "detail" not in payload


def test_internal_error_builder_golden():
    """internal_error() 便捷构造：witty-service 侧合成 stream.error 的统一出口。"""
    assert internal_error("Background WS consumer encountered an error") == {
        "code": "INTERNAL_ERROR",
        "message": "Background WS consumer encountered an error",
        "source": StreamErrorSource.INTERNAL.value,
        "retryable": False,
    }


def test_normalize_stream_error_truncates_and_flattens():
    payload = normalize_stream_error(
        code=StreamErrorCode.UPSTREAM_REQUEST,
        message="line1\nline2  " + "x" * 600,
        source="upstream",
        retryable=False,
        detail={"response_body": "y" * 600},
    )
    assert "\n" not in payload["message"]
    assert len(payload["message"]) == 500
    assert len(payload["detail"]["response_body"]) == 500


# ==============================================================================
# opencode session.error（真实故障样本：Moonshot 401）
# ==============================================================================


def test_normalize_opencode_error_auth_sample():
    raw = {
        "type": "session.error",
        "sessionID": "ses_ee559aaf4ffekbbCr09c2rZCZY",
        "error": {
            "name": "APIError",
            "data": {
                "message": "Invalid Authentication",
                "statusCode": 401,
                "isRetryable": False,
                "responseBody": '{"error":{"message":"Invalid Authentication","type":"invalid_authentication_error"}}',
                "metadata": {"url": "https://api.moonshot.cn/v1/chat/completions"},
            },
        },
    }
    payload = normalize_opencode_error(raw)
    assert payload["code"] == "UPSTREAM_AUTH"
    assert payload["message"] == "Invalid Authentication"
    assert payload["source"] == "upstream"
    assert payload["retryable"] is False
    assert payload["detail"] == {
        "provider_code": "APIError",
        "status_code": 401,
        "url": "https://api.moonshot.cn/v1/chat/completions",
        "response_body": '{"error":{"message":"Invalid Authentication","type":"invalid_authentication_error"}}',
    }


def test_normalize_opencode_error_without_error_object():
    payload = normalize_opencode_error({"type": "question.asked", "data": {}})
    assert payload["code"] == "RUNTIME_PROTOCOL_ERROR"
    assert payload["source"] == "runtime"
    assert "raw_event" in payload["detail"]


def test_normalize_opencode_error_retries_on_5xx_without_flag():
    raw = {
        "error": {"name": "APIError", "data": {"message": "boom", "statusCode": 503}}
    }
    payload = normalize_opencode_error(raw)
    assert payload["code"] == "UPSTREAM_SERVER"
    assert payload["retryable"] is True


# ==============================================================================
# quota 型 429 改判（429 + 配额耗尽关键字 → UPSTREAM_QUOTA_EXCEEDED 不可重试）
# ==============================================================================


def test_normalize_opencode_error_quota_429_not_retryable():
    raw = {
        "error": {
            "name": "APIError",
            "data": {
                "message": "You exceeded your current quota, please check your plan and billing details.",
                "statusCode": 429,
                "isRetryable": True,
            },
        }
    }
    payload = normalize_opencode_error(raw)
    assert payload["code"] == "UPSTREAM_QUOTA_EXCEEDED"
    # 上游误标 isRetryable=True 也强制不可重试：配额耗尽是永久性错误
    assert payload["retryable"] is False


def test_normalize_opencode_error_plain_429_stays_rate_limit():
    raw = {
        "error": {
            "name": "APIError",
            "data": {"message": "Too many requests", "statusCode": 429},
        }
    }
    payload = normalize_opencode_error(raw)
    assert payload["code"] == "UPSTREAM_RATE_LIMIT"
    assert payload["retryable"] is True


def test_normalize_opencode_error_deepseek_402_insufficient_balance():
    """DeepSeek 实测样本：402 + Insufficient Balance 应归配额耗尽而非 4xx 兜底。"""
    raw = {
        "error": {
            "name": "APIError",
            "data": {
                "message": "Insufficient Balance (request_id: f2b501ba-8f7c-4a64-8b71-4a6164c3c034)",
                "statusCode": 402,
                "responseBody": '{"error":{"message":"Insufficient Balance","type":"unknown_error","code":"invalid_request_error"}}',
            },
        }
    }
    payload = normalize_opencode_error(raw)
    assert payload["code"] == "UPSTREAM_QUOTA_EXCEEDED"
    assert payload["retryable"] is False
    assert payload["detail"]["status_code"] == 402


# ==============================================================================
# openclaw lifecycle（真实故障样本："HTTP 401: Invalid Authentication"）
# ==============================================================================


def test_normalize_openclaw_lifecycle_error_http_401():
    payload = normalize_openclaw_lifecycle_error(
        {"phase": "error", "message": "HTTP 401: Invalid Authentication"}
    )
    assert payload["code"] == "UPSTREAM_AUTH"
    assert payload["message"] == "Invalid Authentication"
    assert payload["source"] == "upstream"
    assert payload["retryable"] is False
    assert payload["detail"] == {"status_code": 401}


def test_normalize_openclaw_lifecycle_error_unparsable_message():
    payload = normalize_openclaw_lifecycle_error(
        {"phase": "error", "code": "ERR_X", "message": "something odd happened"}
    )
    assert payload["code"] == "RUNTIME_LIFECYCLE_ERROR"
    assert payload["message"] == "something odd happened"
    assert payload["source"] == "runtime"
    assert payload["detail"] == {"provider_code": "ERR_X"}


def test_normalize_openclaw_lifecycle_error_retries_on_502():
    payload = normalize_openclaw_lifecycle_error({"message": "HTTP 502: Bad Gateway"})
    assert payload["code"] == "UPSTREAM_SERVER"
    assert payload["retryable"] is True


def test_normalize_openclaw_lifecycle_error_quota_429():
    payload = normalize_openclaw_lifecycle_error(
        {"message": "HTTP 429: Insufficient Balance, please recharge."}
    )
    assert payload["code"] == "UPSTREAM_QUOTA_EXCEEDED"
    assert payload["retryable"] is False
    assert payload["detail"] == {"status_code": 429}


def test_normalize_openclaw_lifecycle_error_plain_429_stays_rate_limit():
    payload = normalize_openclaw_lifecycle_error(
        {"message": "HTTP 429: Too many requests"}
    )
    assert payload["code"] == "UPSTREAM_RATE_LIMIT"
    assert payload["retryable"] is True


def test_normalize_openclaw_lifecycle_error_402_insufficient_balance():
    payload = normalize_openclaw_lifecycle_error(
        {"message": "HTTP 402: Insufficient Balance"}
    )
    assert payload["code"] == "UPSTREAM_QUOTA_EXCEEDED"
    assert payload["retryable"] is False


def test_normalize_openclaw_lifecycle_error_free_text_billing():
    """openclaw 实测样本：无 "HTTP NNN" 前缀的自由文本 billing 报错，
    落不到状态分类，须按关键字改判为配额耗尽，与 opencode/dsh 对齐。"""
    payload = normalize_openclaw_lifecycle_error(
        {
            "message": (
                "⚠️ deepseek (deepseek-v4-flash) returned a billing error — "
                "your API key has run out of credits or has an insufficient balance. "
                "Check your deepseek billing dashboard and top up or switch to a "
                "different API key."
            )
        }
    )
    assert payload["code"] == "UPSTREAM_QUOTA_EXCEEDED"
    assert payload["source"] == "upstream"
    assert payload["retryable"] is False
    assert "insufficient balance" in payload["message"]


def test_normalize_openclaw_lifecycle_error_free_text_stays_lifecycle():
    """无配额关键字的自由文本仍保持 lifecycle 兜底分类。"""
    payload = normalize_openclaw_lifecycle_error(
        {"message": "⚠️ tool execution aborted by user"}
    )
    assert payload["code"] == "RUNTIME_LIFECYCLE_ERROR"
    assert payload["source"] == "runtime"
    assert payload["retryable"] is False


# ==============================================================================
# dsh（真实故障样本：AUTH + "api key ... invalid (request_id: ...)"）
# ==============================================================================


def test_normalize_dsh_turn_error_auth_sample():
    error = {
        "code": "AUTH",
        "message": (
            "Authentication Fails, Your api key: ****m/v1 is invalid "
            "(request_id: edac8948-eaee-40a3-99c5-0e44056926c0)"
        ),
        "status": 401,
    }
    payload = normalize_dsh_turn_error(error)
    assert payload["code"] == "UPSTREAM_AUTH"
    assert payload["source"] == "upstream"
    assert payload["retryable"] is False
    assert payload["detail"] == {"provider_code": "AUTH", "status_code": 401}


def test_normalize_dsh_turn_error_code_hint_fallback():
    payload = normalize_dsh_turn_error(
        {"code": "MODEL_NOT_FOUND", "message": "no model"}
    )
    assert payload["code"] == "UPSTREAM_REQUEST"
    assert payload["detail"] == {"provider_code": "MODEL_NOT_FOUND"}


def test_normalize_dsh_turn_error_quota_429():
    error = {
        "code": "RATE_LIMITED",
        "message": "Insufficient quota, please upgrade your plan.",
        "status": 429,
    }
    payload = normalize_dsh_turn_error(error)
    assert payload["code"] == "UPSTREAM_QUOTA_EXCEEDED"
    assert payload["retryable"] is False
    assert payload["detail"] == {"provider_code": "RATE_LIMITED", "status_code": 429}


def test_normalize_dsh_turn_error_402_insufficient_balance():
    error = {
        "code": "INVALID_REQUEST",
        "message": "Insufficient Balance",
        "status": 402,
    }
    payload = normalize_dsh_turn_error(error)
    assert payload["code"] == "UPSTREAM_QUOTA_EXCEEDED"
    assert payload["retryable"] is False
    assert payload["detail"] == {"provider_code": "INVALID_REQUEST", "status_code": 402}


# ==============================================================================
# message 拆包（dsh 上游把整个 JSON 错误体塞进 message 文本，实测样本）
# ==============================================================================


def test_normalize_dsh_turn_error_unwraps_embedded_json_message():
    """dsh 实测样本：message='401: {"message":"Invalid Authentication",...}'
    应拆出内层 message，与 opencode/openclaw 的展示口径对齐。"""
    error = {
        "code": "AUTH",
        "message": '401: {"message":"Invalid Authentication","type":"invalid_authentication_error"}',
        "status": 401,
    }
    payload = normalize_dsh_turn_error(error)
    assert payload["code"] == "UPSTREAM_AUTH"
    assert payload["message"] == "Invalid Authentication"


def test_extract_embedded_error_message_nested_error_object():
    payload = normalize_stream_error(
        code=StreamErrorCode.UPSTREAM_SERVER,
        message='502: {"error":{"message":"upstream connect error","type":"api_error"}}',
        source="upstream",
        retryable=True,
    )
    assert payload["message"] == "upstream connect error"


def test_extract_embedded_error_message_leaves_plain_text_alone():
    """非 `NNN: {json}` 形态、JSON 解析失败、无内层 message 时原样保留。"""
    for message in (
        "Authentication Fails, Your api key is invalid (request_id: x)",
        "401: not-a-json",
        '429: {"status": "weird"}',
    ):
        payload = normalize_stream_error(
            code=StreamErrorCode.UPSTREAM_AUTH,
            message=message,
            source="upstream",
            retryable=False,
        )
        assert payload["message"] == message


def test_normalize_dsh_client_error_slug():
    payload = normalize_dsh_client_error(
        "transport-closed", "dsh stream_turn failed: closed"
    )
    assert payload["code"] == "TRANSPORT_CLOSED"
    assert payload["source"] == "transport"
    assert payload["retryable"] is True
    assert payload["detail"] == {"provider_code": "transport-closed"}


def test_normalize_dsh_client_error_config_invalid_is_permanent():
    """provider 配置错误（config-invalid）是永久性错误：不可重试、
    source 随类别推导为 upstream，而非一律标 transport。"""
    payload = normalize_dsh_client_error(
        "config-invalid", "dsh provider route requires dsh_home"
    )
    assert payload["code"] == "UPSTREAM_REQUEST"
    assert payload["source"] == "upstream"
    assert payload["retryable"] is False


def test_normalize_dsh_client_error_not_started_is_lifecycle():
    payload = normalize_dsh_client_error("not-started", "harness is not started")
    assert payload["code"] == "RUNTIME_LIFECYCLE_ERROR"
    assert payload["source"] == "runtime"
    assert payload["retryable"] is False


def test_normalize_dsh_turn_error_quota_without_status():
    """无状态码、仅 code hint 命中限流的配额耗尽同样改判为不可重试的
    UPSTREAM_QUOTA_EXCEEDED（否则被当成可重试限流反复重试）。"""
    payload = normalize_dsh_turn_error(
        {"code": "QUOTA_EXCEEDED", "message": "Insufficient quota, please upgrade."}
    )
    assert payload["code"] == "UPSTREAM_QUOTA_EXCEEDED"
    assert payload["retryable"] is False
    assert payload["detail"] == {"provider_code": "QUOTA_EXCEEDED"}


# ==============================================================================
# normalize_exception_error（orchestrator 异常路径）
# ==============================================================================


class _StatusError(Exception):
    status_code = 401


class _FakeResponse:
    status_code = 401


class _HttpStatusLikeError(Exception):
    """httpx.HTTPStatusError 形态：状态码挂在 response 上而非异常自身。"""

    response = _FakeResponse()


class _ReasonError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(f"dsh failure: {reason}")
        self.reason = reason


def test_normalize_exception_error_with_status_code():
    payload = normalize_exception_error(_StatusError("HTTP 401 unauthorized"))
    assert payload["code"] == "UPSTREAM_AUTH"
    assert payload["source"] == "upstream"
    assert payload["detail"]["status_code"] == 401


def test_normalize_exception_error_extracts_status_from_response():
    """httpx.HTTPStatusError 无自身 status_code 属性：须从 response.status_code
    提取，否则 401 会被误判成可重试的 TRANSPORT_ERROR。"""
    payload = normalize_exception_error(
        _HttpStatusLikeError("Client error '401 Unauthorized' for url ...")
    )
    assert payload["code"] == "UPSTREAM_AUTH"
    assert payload["source"] == "upstream"
    assert payload["retryable"] is False
    assert payload["detail"]["status_code"] == 401


def test_normalize_exception_error_with_dsh_reason():
    payload = normalize_exception_error(_ReasonError("timeout"))
    assert payload["code"] == StreamErrorCode.UPSTREAM_TIMEOUT
    assert payload["source"] == "upstream"
    assert payload["retryable"] is True


def test_normalize_exception_error_generic_fallback():
    payload = normalize_exception_error(RuntimeError("runtime request failed"))
    assert payload["code"] == "INTERNAL_ERROR"
    assert payload["source"] == "internal"
    assert payload["retryable"] is False


class _OkStatusError(Exception):
    """带 2xx 状态码的异常：classify_from_status 无法分类，走 INTERNAL 兜底。"""

    status_code = 200


def test_normalize_exception_error_unclassifiable_status_source_is_internal():
    """2xx/3xx 状态无法分类时 fallback INTERNAL_ERROR，source 须随类别推导为
    internal——不得产出 INTERNAL_ERROR + source=upstream 的自相矛盾载荷。"""
    payload = normalize_exception_error(_OkStatusError("unexpected 200"))
    assert payload["code"] == "INTERNAL_ERROR"
    assert payload["source"] == "internal"


class _HttpxReadTimeout(Exception):
    """httpx.ReadTimeout 形态：模块名须落在 httpx 下才能命中传输异常嗅探。"""

    __module__ = "httpx._exceptions"


def test_normalize_exception_error_httpx_timeout_source_follows_code():
    """httpx 超时嗅探：source 随类别推导为 upstream，与 dsh timeout slug
    （classify_dsh_reason("timeout") → UPSTREAM_TIMEOUT + upstream）口径一致。"""
    payload = normalize_exception_error(_HttpxReadTimeout("timed out"))
    assert payload["code"] == StreamErrorCode.UPSTREAM_TIMEOUT
    assert payload["source"] == "upstream"
    assert payload["retryable"] is True
