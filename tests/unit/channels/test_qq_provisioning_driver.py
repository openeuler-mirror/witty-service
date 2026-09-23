"""QQ 扫码接入驱动的用例（官方 q.qq.com「lite 绑定任务」）。

协议事实来自官方连接器 `@tencent-connect/qqbot-connector@1.2.0`：三个字段名、
`retcode` 语义、状态枚举 0/1/2/3、AES-256-GCM 的 `IV(12) || 密文 || tag(16)` 布局。
`http_post` 是注入点因此不联网；密文用 `cryptography` 现场生成，而不是抄固定字符串。
"""

from __future__ import annotations

import base64
import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from witty_service.channels.adapters.qq_bot import (
    BIND_STATUS_COMPLETED,
    BIND_STATUS_EXPIRED,
    BIND_STATUS_NONE,
    BIND_STATUS_PENDING,
    QR_CONNECT_PATH,
    QR_CREATE_TASK_PATH,
    QR_POLL_INTERVAL_MS,
    QR_POLL_TASK_PATH,
    QR_SERVICE_BASE,
    QR_TTL_SECONDS,
    QqProvisioningDriver,
    QqProvisioningError,
    QqTransientError,
    _default_http_post,
    build_connect_url,
    decrypt_bind_secret,
    generate_bind_key,
)
from witty_service.channels.provisioning.drivers import (
    DRIVER_REGISTRY,
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    STATUS_WAITING,
)

SOURCE = "witty-service"


class QrService:
    """假扫码服务：`create_bind_task` 依次发任务号，`poll_bind_result` 依次给结果。"""

    def __init__(
        self,
        *,
        tasks: list[str] | None = None,
        results: list[Mapping[str, Any]] | None = None,
        retcode: int = 0,
        message: str = "",
        create_retcode: int | None = None,
        create_data: Mapping[str, Any] | None = None,
    ) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self._tasks = list(tasks or ["TASK_1"])
        self._results = list(results or [{"status": BIND_STATUS_PENDING}])
        self._retcode = retcode
        self._message = message
        self._create_retcode = create_retcode
        self._create_data = create_data

    async def post(self, url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.requests.append((url, dict(payload)))
        if url.endswith(QR_CREATE_TASK_PATH):
            retcode = (
                self._retcode if self._create_retcode is None else self._create_retcode
            )
            if retcode != 0:
                return {
                    "retcode": retcode,
                    "msg": self._message or "nope",
                    "data": None,
                }
            if self._create_data is not None:
                return {"retcode": 0, "data": dict(self._create_data)}
            task = self._tasks.pop(0) if self._tasks else "TASK_X"
            return {"retcode": 0, "data": {"task_id": task}}
        if self._retcode != 0:
            return {
                "retcode": self._retcode,
                "msg": self._message or "nope",
                "data": None,
            }
        result = (
            self._results.pop(0) if self._results else {"status": BIND_STATUS_PENDING}
        )
        return {"retcode": 0, "data": dict(result)}

    def paths(self) -> list[str]:
        return [url.rsplit("/", 1)[-1] for url, _ in self.requests]


def _driver(service: QrService, **kwargs: Any) -> QqProvisioningDriver:
    return QqProvisioningDriver(http_post=service.post, **kwargs)


def _state_of(session_state: bytes) -> dict[str, Any]:
    return json.loads(session_state.decode("utf-8"))


def _encode(task_id: str, key: str, refreshes: int = 0) -> bytes:
    return json.dumps({"task_id": task_id, "key": key, "refreshes": refreshes}).encode(
        "utf-8"
    )


def _encrypt_secret(secret: str, key: str) -> str:
    """按官方连接器的布局加密：IV(12) || ciphertext || tag(16)，整体再 base64。"""
    iv = secrets.token_bytes(12)
    blob = AESGCM(base64.b64decode(key)).encrypt(iv, secret.encode("utf-8"), None)
    ciphertext, tag = blob[:-16], blob[-16:]
    return base64.b64encode(iv + ciphertext + tag).decode("ascii")


# ====================================== 注册 ======================================


def test_driver_is_registered_for_qq() -> None:
    """`/channels/catalog` 的 supports_provisioning 取的就是这张表。"""
    assert DRIVER_REGISTRY["qq_bot"] is QqProvisioningDriver
    assert "wecom_bot" in DRIVER_REGISTRY


# ====================================== begin ======================================


@pytest.mark.asyncio
async def test_begin_creates_a_bind_task_and_returns_a_scannable_url() -> None:
    service = QrService(tasks=["TASK_42"])
    driver = _driver(service)

    session = await driver.begin()

    url, payload = service.requests[0]
    assert url == f"{QR_SERVICE_BASE}{QR_CREATE_TASK_PATH}"
    state = _state_of(session.state)
    assert state["task_id"] == "TASK_42"
    assert state["refreshes"] == 0
    # 申请任务时交出去的 key 与留在服务端的那把必须是同一把（它就是解密的密钥）
    assert payload == {"key": state["key"]}

    assert session.qr_content == (
        f"{QR_SERVICE_BASE}{QR_CONNECT_PATH}?task_id=TASK_42&source={SOURCE}&_wv=2"
    )
    assert session.poll_interval_ms == QR_POLL_INTERVAL_MS == 2000
    assert QR_TTL_SECONDS == 5 * 60


@pytest.mark.asyncio
async def test_begin_expiry_is_the_local_ttl_and_the_key_is_32_bytes() -> None:
    now = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    service = QrService()
    driver = _driver(service, clock=lambda: now)

    session = await driver.begin()

    assert session.expires_at == now + timedelta(seconds=QR_TTL_SECONDS)
    assert len(base64.b64decode(_state_of(session.state)["key"])) == 32


@pytest.mark.asyncio
async def test_every_attempt_uses_a_fresh_key() -> None:
    """key 是 AES 密钥：复用会让"上一次的密文"和"这一次的任务"对得上。"""
    service = QrService()
    driver = _driver(service)

    first = _state_of((await driver.begin()).state)["key"]
    second = _state_of((await driver.begin()).state)["key"]

    assert first != second
    assert generate_bind_key() not in {first, second}


@pytest.mark.asyncio
async def test_the_qr_url_never_carries_the_key() -> None:
    """二维码内容会下发到前端并渲染成图片：里面只能有任务号，不能有解密密钥。"""
    service = QrService()
    driver = _driver(service)

    session = await driver.begin()
    key = _state_of(session.state)["key"]

    assert key not in session.qr_content
    assert build_connect_url("TASK_1") == (
        f"{QR_SERVICE_BASE}{QR_CONNECT_PATH}?task_id=TASK_1&source={SOURCE}&_wv=2"
    )


@pytest.mark.asyncio
async def test_create_task_failure_raises_for_the_flow_to_report() -> None:
    """`begin` 失败由编排层转成"接入服务不可用"，驱动不吞掉它。"""
    service = QrService(retcode=100007, message="invalid key")
    driver = _driver(service)

    with pytest.raises(QqProvisioningError) as caught:
        await driver.begin()

    assert caught.value.code == "QQ_100007"
    assert caught.value.message == "invalid key"


@pytest.mark.asyncio
async def test_create_task_without_a_task_id_is_an_error() -> None:
    """平台回了 200 但没有任务号：必须当成错误，而不是拿空任务号去拼二维码。"""
    service = QrService(create_data={})
    driver = _driver(service)

    with pytest.raises(QqProvisioningError) as caught:
        await driver.begin()

    assert caught.value.code == "QQ_NO_TASK"


# =================================== poll：进行中 ===================================


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [BIND_STATUS_NONE, BIND_STATUS_PENDING])
async def test_pending_statuses_keep_waiting(status: int) -> None:
    service = QrService(results=[{"status": status}])
    driver = _driver(service)

    outcome = await driver.poll(_encode("TASK_1", generate_bind_key()))

    assert outcome.status == STATUS_WAITING
    assert outcome.credentials is None
    assert outcome.state is None
    assert outcome.qr_content is None
    assert service.requests[0][0] == f"{QR_SERVICE_BASE}{QR_POLL_TASK_PATH}"
    assert service.requests[0][1] == {"task_id": "TASK_1"}


def _stub_httpx_client(monkeypatch, handler) -> None:
    """让 `_default_http_post` 内部自建的 AsyncClient 走 MockTransport。

    这个函数是唯一真正碰网络的实现（它自己建 client，没有注入点），因此只能替换
    `httpx.AsyncClient` 本身；被断言的仍然是**真实**的 httpx 异常层次。
    """
    real_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_default_http_post_maps_network_failures_to_transient(
    monkeypatch,
) -> None:
    """ReadTimeout / ConnectError 都是"没能拿到答复"，必须归到可重试那一支。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    _stub_httpx_client(monkeypatch, handler)

    with pytest.raises(QqTransientError) as caught:
        await _default_http_post("https://q.qq.com/lite/poll_bind_result", {})

    assert caught.value.code == "HTTP_ERROR"
    # 仍然是 QqProvisioningError：忘了分类的调用点会退化成"失败"，但不会冒到接口层
    assert isinstance(caught.value, QqProvisioningError)


@pytest.mark.asyncio
async def test_default_http_post_maps_error_statuses_to_deterministic(
    monkeypatch,
) -> None:
    """服务端明确回了状态码：重试没有意义，不能算瞬时错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "oops"})

    _stub_httpx_client(monkeypatch, handler)

    with pytest.raises(QqProvisioningError) as caught:
        await _default_http_post("https://q.qq.com/lite/poll_bind_result", {})

    assert not isinstance(caught.value, QqTransientError)
    assert caught.value.code == "HTTP_STATUS"


@pytest.mark.asyncio
async def test_unknown_status_is_a_failure() -> None:
    service = QrService(results=[{"status": 99}])
    driver = _driver(service)

    outcome = await driver.poll(_encode("TASK_1", generate_bind_key()))

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "QQ_BAD_STATUS"


def _raising_post(exc: BaseException):
    async def post(url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise exc

    return post


@pytest.mark.asyncio
async def test_a_transient_poll_error_keeps_the_attempt_waiting() -> None:
    """回归：一次网络抖动不是"接入失败"——二维码在平台上还有效，不能就此写终态。

    旧实现把它当确定性失败写进终态并删掉状态文件，用户只能重新扫一次码。
    """
    driver = QqProvisioningDriver(
        http_post=_raising_post(QqTransientError("HTTP_ERROR", "read timeout"))
    )

    outcome = await driver.poll(_encode("TASK_1", generate_bind_key()))

    assert outcome.status == STATUS_WAITING
    assert outcome.error_code is None
    # 状态与二维码都不变：下一次轮询继续用同一个任务
    assert outcome.state is None
    assert outcome.qr_content is None


@pytest.mark.asyncio
async def test_a_deterministic_poll_error_still_fails_the_attempt() -> None:
    """两类错误必须分得开：平台明确表态的失败仍然写终态，否则前端会一直轮询。"""
    driver = QqProvisioningDriver(
        http_post=_raising_post(QqProvisioningError("HTTP_STATUS", "500"))
    )

    outcome = await driver.poll(_encode("TASK_1", generate_bind_key()))

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "HTTP_STATUS"


@pytest.mark.asyncio
async def test_a_transient_error_while_refreshing_also_keeps_waiting() -> None:
    """换二维码时抖动：这次尝试仍然 waiting，下一次轮询会再试一次。"""

    async def post(url: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if url.endswith(QR_POLL_TASK_PATH):
            return {"retcode": 0, "data": {"status": BIND_STATUS_EXPIRED}}
        raise QqTransientError("HTTP_ERROR", "connection reset")

    driver = QqProvisioningDriver(http_post=post)

    outcome = await driver.poll(_encode("TASK_1", generate_bind_key()))

    assert outcome.status == STATUS_WAITING
    assert outcome.state is None


@pytest.mark.asyncio
async def test_bad_state_is_a_failure_not_an_exception() -> None:
    """状态文件损坏时"这次尝试失败"就够了，不该把 500 冒到接口层。"""
    service = QrService()
    driver = _driver(service)

    for state in (b"not json", b"{}", json.dumps({"task_id": "T"}).encode()):
        outcome = await driver.poll(state)
        assert outcome.status == STATUS_FAILED
        assert outcome.error_code == "QQ_BAD_STATE"
    assert service.requests == []


@pytest.mark.asyncio
async def test_retcode_failure_fails_the_attempt() -> None:
    service = QrService(retcode=100007, message="invalid task")
    driver = _driver(service)

    outcome = await driver.poll(_encode("TASK_1", generate_bind_key()))

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "QQ_100007"


# ========================== poll：扫码完成（AppSecret 是加密的） ==========================


@pytest.mark.asyncio
async def test_completed_status_returns_the_decrypted_credentials() -> None:
    key = generate_bind_key()
    service = QrService(
        results=[
            {
                "status": BIND_STATUS_COMPLETED,
                "bot_appid": "102123456",
                "bot_encrypt_secret": _encrypt_secret("s3cr3t-value", key),
                "user_openid": "OPENID_SCANNER",
            }
        ]
    )
    driver = _driver(service)

    outcome = await driver.poll(_encode("TASK_1", key))

    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.credentials == {
        "app_id": "102123456",
        "secret": "s3cr3t-value",
        # 扫码人的 openid：四个渠道里唯一能拿到的"所有者身份"
        "owner_user_openid": "OPENID_SCANNER",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["2", BIND_STATUS_COMPLETED])
async def test_completed_status_accepts_numeric_strings(status: object) -> None:
    """平台给的是数字；多认一种写法只是为了让"已经扫上了"不会被误判成未知状态。"""
    key = generate_bind_key()
    service = QrService(
        results=[
            {
                "status": status,
                "bot_appid": "102123456",
                "bot_encrypt_secret": _encrypt_secret("s3cr3t", key),
            }
        ]
    )
    driver = _driver(service)

    outcome = await driver.poll(_encode("TASK_1", key))

    assert outcome.status == STATUS_SUCCEEDED


@pytest.mark.asyncio
async def test_completed_status_accepts_the_documented_field_names() -> None:
    """连接器读 `bot_appid`，接口文档写作 `bot_app_id`：两个都认。"""
    key = generate_bind_key()
    service = QrService(
        results=[
            {
                "status": BIND_STATUS_COMPLETED,
                "bot_app_id": "102123456",
                "encrypt_secret": _encrypt_secret("s3cr3t", key),
            }
        ]
    )
    driver = _driver(service)

    outcome = await driver.poll(_encode("TASK_1", key))

    assert outcome.status == STATUS_SUCCEEDED
    assert outcome.credentials is not None
    assert outcome.credentials["app_id"] == "102123456"
    assert outcome.credentials["secret"] == "s3cr3t"
    # 平台没给扫码人就不编一个出来
    assert "owner_user_openid" not in outcome.credentials


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data",
    [
        {"status": BIND_STATUS_COMPLETED},
        {"status": BIND_STATUS_COMPLETED, "bot_appid": "102123456"},
        {"status": BIND_STATUS_COMPLETED, "bot_encrypt_secret": "AAAA"},
    ],
)
async def test_completed_without_both_fields_fails(data: dict[str, Any]) -> None:
    service = QrService(results=[data])
    driver = _driver(service)

    outcome = await driver.poll(_encode("TASK_1", generate_bind_key()))

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "QQ_MISSING_CREDENTIALS"


@pytest.mark.asyncio
async def test_undecryptable_secret_fails_instead_of_storing_garbage() -> None:
    """解不开就是解不开：宁可让这次接入失败，也不能把一个空密码落进凭据文件。"""
    key = generate_bind_key()
    other = generate_bind_key()
    service = QrService(
        results=[
            {
                "status": BIND_STATUS_COMPLETED,
                "bot_appid": "102123456",
                # 用别的 key 加密：模拟"密文与任务对不上"
                "bot_encrypt_secret": _encrypt_secret("s3cr3t", other),
            }
        ]
    )
    driver = _driver(service)

    outcome = await driver.poll(_encode("TASK_1", key))

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "QQ_BAD_SECRET"


# =================== poll：二维码过期 -> 换任务（同一个接入尝试继续） ===================


@pytest.mark.asyncio
async def test_expired_status_rotates_the_qr_and_the_state() -> None:
    now = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
    # 假服务按调用顺序发任务号：`begin` 已经用掉第一个，因此这里只剩"刷新时新建的那个"
    service = QrService(tasks=["TASK_2"], results=[{"status": BIND_STATUS_EXPIRED}])
    driver = _driver(service, clock=lambda: now)

    outcome = await driver.poll(_encode("TASK_1", generate_bind_key()))

    assert outcome.status == STATUS_WAITING
    # 新二维码：新任务号 + 重新计时的有效期
    assert outcome.qr_content == (
        f"{QR_SERVICE_BASE}{QR_CONNECT_PATH}?task_id=TASK_2&source={SOURCE}&_wv=2"
    )
    assert outcome.expires_at == now + timedelta(seconds=QR_TTL_SECONDS)
    assert outcome.state is not None
    state = _state_of(outcome.state)
    assert state["task_id"] == "TASK_2"
    assert state["refreshes"] == 1
    # 换任务必须重新申请：不能拿着旧任务号去改 URL
    assert service.paths() == ["poll_bind_result", "create_bind_task"]


@pytest.mark.asyncio
async def test_rotation_keeps_the_refresh_counter() -> None:
    service = QrService(
        tasks=["TASK_2", "TASK_3", "TASK_4"],
        results=[{"status": BIND_STATUS_EXPIRED}, {"status": BIND_STATUS_EXPIRED}],
    )
    driver = _driver(service)

    first = await driver.poll(_encode("TASK_1", generate_bind_key(), refreshes=4))
    assert first.state is not None
    assert _state_of(first.state)["refreshes"] == 5

    second = await driver.poll(first.state)
    assert second.state is not None
    assert _state_of(second.state)["refreshes"] == 6


@pytest.mark.asyncio
async def test_refresh_cap_ends_the_attempt_as_expired() -> None:
    """被遗弃的对话框不该无限申请绑定任务：到上限就如实报过期。"""
    service = QrService(tasks=["TASK_2"], results=[{"status": BIND_STATUS_EXPIRED}])
    driver = _driver(service, max_refreshes=1)

    outcome = await driver.poll(_encode("TASK_1", generate_bind_key(), refreshes=1))

    assert outcome.status == STATUS_EXPIRED
    assert service.paths() == ["poll_bind_result"]


@pytest.mark.asyncio
async def test_rotation_failure_fails_the_attempt() -> None:
    service = QrService(
        tasks=["TASK_2"],
        results=[{"status": BIND_STATUS_EXPIRED}],
        create_retcode=100007,
    )
    driver = _driver(service)

    outcome = await driver.poll(_encode("TASK_1", generate_bind_key()))

    assert outcome.status == STATUS_FAILED
    assert outcome.error_code == "QQ_100007"


# ===================================== 解密本身 ====================================


def test_decrypt_bind_secret_round_trips() -> None:
    key = generate_bind_key()

    assert decrypt_bind_secret(_encrypt_secret("hello-世界", key), key) == "hello-世界"


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # 太短：连 IV + tag 都放不下
        (base64.b64encode(b"too-short").decode("ascii"), ValueError),
        # 长度够但 GCM 认证标签对不上：解出乱码必须失败，不能把乱码当密钥
        (base64.b64encode(secrets.token_bytes(64)).decode("ascii"), InvalidTag),
    ],
)
def test_decrypt_bind_secret_rejects_bad_payloads(
    payload: str, expected: type[Exception]
) -> None:
    with pytest.raises(expected):
        decrypt_bind_secret(payload, generate_bind_key())


def test_decrypt_bind_secret_rejects_a_wrong_length_key() -> None:
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")

    with pytest.raises(ValueError):
        decrypt_bind_secret(_encrypt_secret("s3cr3t", generate_bind_key()), key)
