"""企微扫码接入驱动（官方 device flow）与默认 HTTP 通道的测试。

协议事实（已实测复核）：

    GET https://work.weixin.qq.com/ai/qc/generate?source=..&plat=..
        -> {"data":{"scode":"...","auth_url":"https://work.weixin.qq.com/ai/qc/c?s=..."}}
    GET https://work.weixin.qq.com/ai/qc/query_result?scode=..
        -> {"data":{"status":"...","bot_info":{"botid","secret"}}}

早期猜测的 `qyapi.weixin.qq.com/cgi-bin/channel/provision/qr` 返回 404，已废弃。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from witty_service.channels.adapters import wecom_bot
from witty_service.channels.adapters.wecom_bot import (
    QR_GENERATE_PATH,
    QR_POLL_INTERVAL_MS,
    QR_POLL_PATH,
    QR_SERVICE_BASE,
    QR_TTL_SECONDS,
    WecomProtocolError,
    WecomProvisioningDriver,
    _default_http_get,
    default_platform_code,
)
from witty_service.channels.provisioning.drivers import (
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_WAITING,
)

AUTH_URL = "https://work.weixin.qq.com/ai/qc/c?s=scode-1&hide_more_btn=true"


class Recorder:
    """可编程的 `http_get`：记录请求 URL 并返回预设响应。"""

    def __init__(self, *responses: dict[str, Any]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []
        self.error: BaseException | None = None

    async def __call__(self, url: str) -> dict[str, Any]:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.responses.pop(0) if self.responses else {}

    def params(self, index: int) -> dict[str, list[str]]:
        from urllib.parse import parse_qs, urlparse

        return parse_qs(urlparse(self.urls[index]).query)


def _driver(recorder: Recorder, **kwargs: Any) -> WecomProvisioningDriver:
    return WecomProvisioningDriver(
        api_base=QR_SERVICE_BASE,
        http_get=recorder,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        platform=3,
        **kwargs,
    )


# ==============================================================================
# begin
# ==============================================================================


@pytest.mark.asyncio
async def test_begin_requests_qr_and_keeps_state_server_side() -> None:
    recorder = Recorder({"data": {"scode": "scode-1", "auth_url": AUTH_URL}})
    session = await _driver(recorder).begin()

    assert session.qr_content == AUTH_URL
    assert session.poll_interval_ms == QR_POLL_INTERVAL_MS == 3000
    assert session.expires_at == datetime(2026, 1, 1, tzinfo=UTC) + timedelta(
        seconds=QR_TTL_SECONDS
    )
    # 平台临时凭据（scode）只放在 state 里，由 ProvisioningFlow 加密落库
    assert json.loads(session.state.decode()) == {"scode": "scode-1"}
    # 端点与参数：不在 qyapi 上，必须带 source 与 plat
    assert recorder.urls[0].startswith(f"{QR_SERVICE_BASE}{QR_GENERATE_PATH}?")
    assert recorder.params(0)["source"] == ["witty-service"]
    assert recorder.params(0)["plat"] == ["3"]


@pytest.mark.asyncio
async def test_begin_rejects_auth_url_outside_the_official_host() -> None:
    """auth_url 必须落在 work.weixin.qq.com：避免把用户引到别处（官方实现同款校验）。"""
    recorder = Recorder(
        {"data": {"scode": "s", "auth_url": "https://evil.example.com/ai/qc/c?s=x"}}
    )
    with pytest.raises(WecomProtocolError) as excinfo:
        await _driver(recorder).begin()
    assert excinfo.value.code == "BAD_RESPONSE"


@pytest.mark.asyncio
async def test_begin_rejects_response_without_scode() -> None:
    recorder = Recorder({"data": {"auth_url": AUTH_URL}})
    with pytest.raises(WecomProtocolError) as excinfo:
        await _driver(recorder).begin()
    assert excinfo.value.code == "BAD_RESPONSE"


def test_platform_code_defaults_by_os() -> None:
    # 官方实现：win32 -> 2，linux -> 3，其余 -> 1
    assert default_platform_code() in (1, 2, 3)


# ==============================================================================
# poll
# ==============================================================================


async def _poll(state: dict[str, Any], recorder: Recorder):  # type: ignore[no-untyped-def]
    return await _driver(recorder).poll(json.dumps(state).encode())


@pytest.mark.asyncio
async def test_poll_success_returns_credentials() -> None:
    recorder = Recorder(
        {
            "data": {
                "status": "success",
                "bot_info": {"botid": "bot-1", "secret": "secret-1"},
            }
        }
    )
    outcome = await _poll({"scode": "scode-9"}, recorder)

    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.credentials == {"bot_id": "bot-1", "secret": "secret-1"}
    assert recorder.params(0)["scode"] == ["scode-9"]
    assert recorder.urls[0].startswith(f"{QR_SERVICE_BASE}{QR_POLL_PATH}?")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("scanned", STATUS_WAITING),
        ("waiting", STATUS_WAITING),
        ("", STATUS_WAITING),
        ("expired", STATUS_EXPIRED),
        ("timeout", STATUS_EXPIRED),
        ("fail", STATUS_FAILED),
        ("rejected", STATUS_FAILED),
    ],
)
async def test_poll_status_mapping(status: str, expected: str) -> None:
    outcome = await _poll({"scode": "s"}, Recorder({"data": {"status": status}}))
    assert outcome.status == expected


@pytest.mark.asyncio
async def test_poll_success_without_bot_info_is_failed() -> None:
    outcome = await _poll({"scode": "s"}, Recorder({"data": {"status": "success"}}))
    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "WECOM_MISSING_CREDENTIALS"


@pytest.mark.asyncio
async def test_poll_platform_errcode_is_failed() -> None:
    outcome = await _poll(
        {"scode": "s"}, Recorder({"errcode": 40001, "errmsg": "invalid scode"})
    )
    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "40001"


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [b"not-json", b"{}"])
async def test_poll_with_corrupt_state_is_failed(state: bytes) -> None:
    outcome = await _driver(Recorder()).poll(state)
    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "WECOM_BAD_STATE"


# ==============================================================================
# 默认 HTTP 通道：平台错误必须变成协议错误，而不是 httpx 异常冒泡
# ==============================================================================


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.request = httpx.Request("GET", "https://example.invalid/x")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=self.request, response=self  # type: ignore[arg-type]
            )

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    def __init__(self, response: _FakeResponse | BaseException) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def get(self, url: str, headers: Any = None) -> _FakeResponse:
        del url, headers
        if isinstance(self._response, BaseException):
            raise self._response
        return self._response


def _patch_client(monkeypatch, response: _FakeResponse | BaseException) -> None:
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: _FakeClient(response))


@pytest.mark.asyncio
async def test_default_http_get_maps_http_status_error(monkeypatch) -> None:
    _patch_client(monkeypatch, _FakeResponse(status_code=404))
    with pytest.raises(WecomProtocolError) as excinfo:
        await _default_http_get("https://example.invalid/x")
    assert excinfo.value.code == "HTTP_STATUS"


@pytest.mark.asyncio
async def test_default_http_get_maps_network_error(monkeypatch) -> None:
    _patch_client(monkeypatch, httpx.ConnectError("boom"))
    with pytest.raises(WecomProtocolError) as excinfo:
        await _default_http_get("https://example.invalid/x")
    assert excinfo.value.code == "HTTP_ERROR"


@pytest.mark.asyncio
async def test_default_http_get_maps_non_json_body(monkeypatch) -> None:
    _patch_client(monkeypatch, _FakeResponse(payload=ValueError("not json")))
    with pytest.raises(WecomProtocolError) as excinfo:
        await _default_http_get("https://example.invalid/x")
    assert excinfo.value.code == "BAD_RESPONSE"


@pytest.mark.asyncio
async def test_default_http_get_rejects_non_object_body(monkeypatch) -> None:
    _patch_client(monkeypatch, _FakeResponse(payload=["nope"]))
    with pytest.raises(WecomProtocolError) as excinfo:
        await _default_http_get("https://example.invalid/x")
    assert excinfo.value.code == "BAD_RESPONSE"


def test_driver_is_registered_for_wecom() -> None:
    from witty_service.channels.provisioning.drivers import get_driver_class

    assert get_driver_class("wecom_bot") is wecom_bot.WecomProvisioningDriver
