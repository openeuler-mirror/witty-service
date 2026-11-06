"""stream_errors 归一化模块单测。

三个真实故障样本（配置错误模型后的 401）驱动分类与载荷金样断言。
"""

from __future__ import annotations

import pytest

from witty_service.runtime.stream_errors import (
    StreamErrorCode,
    StreamErrorSource,
    classify_from_code_hint,
    classify_from_status,
    internal_error,
    normalize_exception_error,
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
# message 拆包（上游把整个 JSON 错误体塞进 message 文本，实测样本）
# ==============================================================================


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
    """httpx 超时嗅探：source 随类别推导为 upstream。"""
    payload = normalize_exception_error(_HttpxReadTimeout("timed out"))
    assert payload["code"] == StreamErrorCode.UPSTREAM_TIMEOUT
    assert payload["source"] == "upstream"
    assert payload["retryable"] is True
