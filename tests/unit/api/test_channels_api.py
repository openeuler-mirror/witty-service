"""W15 REST 接口的测试（框架设计 §9）。

用**真实容器 + 真实仓储（临时 SQLite）** 驱动：只有渠道网关与接入驱动是替身，
因此这里同时覆盖了序列化、域错误 -> HTTP 状态码映射、鉴权与"写入后立即生效"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

import witty_service.config as _config
from tests.unit.channels.fakes import FakeAdapter
from witty_service.api.services import ServiceContainer
from witty_service.channels import commands as cmd
from witty_service.channels.contracts import InboundMessage, Route
from witty_service.channels.provisioning.drivers import (
    ProvisioningOutcome,
    ProvisioningSession,
)
from witty_service.channels.provisioning.flow import ProvisioningFlow
from witty_service.main import create_app
from witty_service.persistence.db import create_session_factory, create_sqlite_engine
from witty_service.persistence.orm import Base

SECRET_KEY = Fernet.generate_key().decode("utf-8")
AUTH = {"Authorization": "Bearer test-token"}
BOT_ID = "bot-1234567890"
BOT_SECRET = "super-secret-value-xyz"
QR_CONTENT = "https://open.work.weixin.qq.com/qr/abcdef"
TEMP_STATE = "platform-temp-scode-1"


class FakeGateway:
    """渠道网关替身：只实现接口层用到的那几个方法。"""

    def __init__(
        self,
        *,
        running: bool = True,
        guard_reason: str | None = None,
        connectable: bool = True,
    ) -> None:
        self.running = running
        self.guard_reason = guard_reason
        self.connectable = connectable
        self.connected: set[str] = set()
        self.tests: list[tuple[str, str, str]] = []
        self.disconnected: list[str] = []
        self.reconnects: list[str] = []
        self.test_result: object = None

    def is_connected(self, instance_id: str) -> bool:
        return instance_id in self.connected

    async def connect_instance(self, instance_id: str) -> bool:
        if not self.connectable:
            return False
        self.connected.add(instance_id)
        return True

    async def reconnect_instance(self, instance_id: str) -> bool:
        self.reconnects.append(instance_id)
        self.connected.add(instance_id)
        return True

    async def disconnect_instance(self, instance_id: str) -> None:
        self.disconnected.append(instance_id)
        self.connected.discard(instance_id)

    async def send_test_message(
        self,
        *,
        instance_id: str,
        platform_user_id: str,
        conversation_type: str,
        text: str,
    ) -> object:
        from witty_service.channels.contracts import DeliveryResult

        self.tests.append((instance_id, platform_user_id, text))
        if self.test_result is not None:
            return self.test_result
        return DeliveryResult.delivered_with("platform-msg-1")


class FakeDriver:
    channel = "wecom_bot"

    def __init__(self, outcome: ProvisioningOutcome | None = None) -> None:
        self.outcome = outcome or ProvisioningOutcome(status="waiting")
        self.polled: list[bytes] = []

    async def begin(self) -> ProvisioningSession:
        return ProvisioningSession(
            qr_content=QR_CONTENT,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            poll_interval_ms=0,
            state=TEMP_STATE.encode(),
        )

    async def poll(self, state: bytes) -> ProvisioningOutcome:
        self.polled.append(state)
        return self.outcome


@dataclass
class Env:
    client: TestClient
    services: ServiceContainer
    gateway: FakeGateway
    driver: FakeDriver
    adapter: FakeAdapter = field(default_factory=FakeAdapter)

    def create_instance(self, **payload: object) -> dict:
        body: dict[str, object] = {
            "channel": "wecom_bot",
            "credentials": {"bot_id": BOT_ID, "secret": BOT_SECRET},
        }
        body.update(payload)
        response = self.client.post("/channels/instances", json=body, headers=AUTH)
        assert response.status_code == 201, response.text
        return response.json()

    def register_router_instance(self, instance_id: str, *, agent_id: str | None) -> None:
        record = self.services.channel_repository.get_instance(instance_id)
        assert record is not None
        self.services.channel_router.register_instance(
            instance_id,
            channel=record.channel,
            adapter=self.adapter,
            generation=record.generation,
            agent_id=agent_id,
        )

    async def send_inbound(self, instance_id: str, user: str, *, event_id: str) -> None:
        await self.services.channel_router.handle_inbound(
            InboundMessage(
                platform_event_id=event_id,
                route=Route(
                    instance_id=instance_id,
                    conversation_type="direct",
                    platform_user_id=user,
                ),
                text="你好",
                received_at=datetime.now(UTC),
            )
        )


def _build(
    tmp_path,
    monkeypatch,
    *,
    gateway_running: bool = True,
    outcome: ProvisioningOutcome | None = None,
) -> Env:
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    monkeypatch.setenv("WITTY_CHANNEL_SECRET_KEY", SECRET_KEY)
    monkeypatch.setattr(_config, "_settings", None)

    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'channels-api.sqlite3'}")
    Base.metadata.create_all(engine)
    from witty_service.persistence.repositories import SqliteRepository

    repository = SqliteRepository(create_session_factory(engine))
    services = ServiceContainer(repository=repository, workspace_store=MagicMock())

    gateway = FakeGateway(running=gateway_running)
    driver = FakeDriver(outcome)
    services.channel_gateway = gateway  # type: ignore[assignment]
    services.channel_provisioning = ProvisioningFlow(
        repository=services.channel_repository,
        cipher=services.get_channel_cipher(),
        driver_factory=lambda _channel: driver,
        on_instance_ready=services.notify_channel_instance_ready,
    )
    client = TestClient(create_app(services=services))
    return Env(client=client, services=services, gateway=gateway, driver=driver)


# ==============================================================================
# 鉴权与目录
# ==============================================================================


def test_endpoints_require_bearer_auth(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    assert env.client.get("/channels/instances").status_code == 401
    assert env.client.get("/channels/catalog").status_code == 401
    assert (
        env.client.get("/channels/instances", headers={"Authorization": "Bearer nope"})
        .status_code
        == 401
    )


def test_catalog_is_driven_by_the_adapter_declaration(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    response = env.client.get("/channels/catalog", headers=AUTH)
    assert response.status_code == 200
    items = {item["channel"]: item for item in response.json()}

    assert "wecom_bot" in items  # 取值域来自 ADAPTER_REGISTRY
    wecom = items["wecom_bot"]
    assert wecom["display_name"]
    assert wecom["supports_provisioning"] is True
    assert [field["name"] for field in wecom["credential_fields"]] == [
        "bot_id",
        "secret",
        "ws_url",
    ]
    assert [field["required"] for field in wecom["credential_fields"]] == [
        True,
        True,
        False,
    ]
    assert items["wecom_bot"]["capabilities"]["max_text_length"] > 0


# ==============================================================================
# 手填凭据接入与实例管理
# ==============================================================================


def test_manual_provisioning_returns_mask_only(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    instance = env.create_instance(owner_ref="team-a", agent_id="agent-1")

    assert instance["channel"] == "wecom_bot"
    assert instance["credential_mask"] == "bot-****7890"
    assert instance["owner_ref"] == "team-a"
    assert instance["agent_id"] == "agent-1"
    assert instance["agent_state"] == "deleted"  # 库中没有该 agent
    assert instance["status"] == "pending"
    # 非密配置照常返回，密文字段绝不出现
    assert instance["config"] == {"bot_id": BOT_ID}
    assert BOT_SECRET not in env.client.get("/channels/instances", headers=AUTH).text


def test_manual_provisioning_requires_declared_fields(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    response = env.client.post(
        "/channels/instances",
        json={"channel": "wecom_bot", "credentials": {"bot_id": BOT_ID}},
        headers=AUTH,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CHANNEL_CREDENTIALS_INVALID"


def test_manual_provisioning_rejects_unknown_channel(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    response = env.client.post(
        "/channels/instances",
        json={"channel": "not_a_channel", "credentials": {"secret": "x"}},
        headers=AUTH,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CHANNEL_ADAPTER_UNKNOWN"


def test_list_get_patch_delete_instance(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    instance = env.create_instance()
    instance_id = instance["id"]

    listed = env.client.get("/channels/instances", headers=AUTH).json()
    assert [item["id"] for item in listed] == [instance_id]
    assert env.client.get(
        "/channels/instances", params={"owner_ref": "nobody"}, headers=AUTH
    ).json() == []

    assert (
        env.client.get(f"/channels/instances/{instance_id}", headers=AUTH).status_code
        == 200
    )
    missing = env.client.get("/channels/instances/nope", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "CHANNEL_INSTANCE_NOT_FOUND"

    patched = env.client.patch(
        f"/channels/instances/{instance_id}",
        json={"display_name": "研发助手", "agent_id": "agent-9"},
        headers=AUTH,
    ).json()
    assert patched["display_name"] == "研发助手"
    assert patched["agent_id"] == "agent-9"

    # agent_id=null 表示解绑
    unbound = env.client.patch(
        f"/channels/instances/{instance_id}", json={"agent_id": None}, headers=AUTH
    ).json()
    assert unbound["agent_id"] is None
    assert unbound["agent_state"] == "unbound"
    assert unbound["display_name"] == "研发助手"  # 未提供的字段保持不变

    assert (
        env.client.delete(f"/channels/instances/{instance_id}", headers=AUTH).status_code
        == 204
    )
    assert (
        env.client.get(f"/channels/instances/{instance_id}", headers=AUTH).status_code
        == 404
    )
    # 删除前先断开连接
    assert env.gateway.disconnected == [instance_id]


def test_delete_disconnects_before_row_is_removed(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    instance = env.create_instance()
    env.gateway.connected.add(instance["id"])
    env.client.delete(f"/channels/instances/{instance['id']}", headers=AUTH)
    assert env.gateway.connected == set()


# ==============================================================================
# 扫码接入
# ==============================================================================


def test_provision_begin_poll_and_cancel(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    begun = env.client.post(
        "/channels/provision/begin",
        json={"channel": "wecom_bot", "owner_ref": "team-a", "agent_id": "agent-1"},
        headers=AUTH,
    )
    assert begun.status_code == 200
    payload = begun.json()
    assert payload["status"] == "waiting"
    assert payload["qr_content"] == QR_CONTENT
    assert payload["poll_interval_ms"] == 0
    assert payload["instance"] is None
    # 平台侧临时凭据只存在于服务端
    assert TEMP_STATE not in begun.text

    attempt_id = payload["attempt_id"]
    waiting = env.client.get(f"/channels/provision/{attempt_id}", headers=AUTH).json()
    assert waiting["status"] == "waiting"

    cancelled = env.client.post(
        f"/channels/provision/{attempt_id}/cancel", headers=AUTH
    ).json()
    assert cancelled["status"] == "cancelled"


def test_provision_begin_is_idempotent_for_same_owner(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    body = {"channel": "wecom_bot", "owner_ref": "team-a"}
    first = env.client.post("/channels/provision/begin", json=body, headers=AUTH).json()
    second = env.client.post("/channels/provision/begin", json=body, headers=AUTH).json()
    # 重复发起返回既有尝试，而不是报错（同一实例只允许一个进行中尝试）
    assert first["attempt_id"] == second["attempt_id"]
    assert first["qr_content"] == second["qr_content"]


def test_provision_success_returns_instance_without_plaintext(
    tmp_path, monkeypatch
) -> None:
    env = _build(
        tmp_path,
        monkeypatch,
        outcome=ProvisioningOutcome(
            status="succeeded",
            credentials={"bot_id": BOT_ID, "secret": BOT_SECRET},
        ),
    )
    started = env.client.post(
        "/channels/provision/begin", json={"channel": "wecom_bot"}, headers=AUTH
    ).json()
    polled = env.client.get(
        f"/channels/provision/{started['attempt_id']}", headers=AUTH
    )
    assert polled.status_code == 200
    body = polled.json()

    assert body["status"] == "succeeded"
    assert body["instance"] is not None
    assert body["instance"]["credential_mask"] == "bot-****7890"
    assert BOT_SECRET not in polled.text
    assert TEMP_STATE not in polled.text
    assert env.driver.polled == [TEMP_STATE.encode()]


def test_provision_failure_reports_error_code(tmp_path, monkeypatch) -> None:
    env = _build(
        tmp_path,
        monkeypatch,
        outcome=ProvisioningOutcome(status="failed", error_code="CHANNEL_PROVISIONING_FAILED"),
    )
    started = env.client.post(
        "/channels/provision/begin", json={"channel": "wecom_bot"}, headers=AUTH
    ).json()
    body = env.client.get(
        f"/channels/provision/{started['attempt_id']}", headers=AUTH
    ).json()
    assert body["status"] == "failed"
    assert body["error_code"] == "CHANNEL_PROVISIONING_FAILED"
    assert body["instance"] is None


def test_provision_unknown_attempt_is_404(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    response = env.client.get("/channels/provision/nope", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CHANNEL_PROVISIONING_NOT_FOUND"


def test_provision_unknown_channel_is_rejected(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    response = env.client.post(
        "/channels/provision/begin", json={"channel": "nope"}, headers=AUTH
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CHANNEL_ADAPTER_UNKNOWN"


# ==============================================================================
# 重连与连通性测试
# ==============================================================================


def test_reconnect_requires_a_running_gateway(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch, gateway_running=False)
    env.gateway.guard_reason = "worker_count_not_one"
    instance = env.create_instance()

    response = env.client.post(
        f"/channels/instances/{instance['id']}/reconnect", headers=AUTH
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "CHANNEL_GATEWAY_DISABLED"
    assert error["details"]["reason"] == "worker_count_not_one"
    assert env.gateway.reconnects == []


def test_reconnect_returns_the_instance(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    instance = env.create_instance()
    response = env.client.post(
        f"/channels/instances/{instance['id']}/reconnect", headers=AUTH
    )
    assert response.status_code == 200
    assert response.json()["connected"] is True
    assert env.gateway.reconnects == [instance["id"]]


def test_connectivity_test_sends_fixed_copy(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    instance = env.create_instance()
    response = env.client.post(
        f"/channels/instances/{instance['id']}/test",
        json={"platform_user_id": "u1"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["certainty"] == "delivered"
    assert body["platform_message_ref"] == "platform-msg-1"
    assert env.gateway.tests == [(instance["id"], "u1", cmd.CONNECTIVITY_TEST_TEXT)]


def test_connectivity_test_surfaces_rejected_and_uncertain(tmp_path, monkeypatch) -> None:
    from witty_service.channels.contracts import DeliveryResult

    env = _build(tmp_path, monkeypatch)
    instance = env.create_instance()
    for result, certainty, code in (
        (
            DeliveryResult.rejected_with("CHANNEL_CREDENTIAL_INVALID"),
            "rejected",
            "CHANNEL_CREDENTIAL_INVALID",
        ),
        (
            DeliveryResult.uncertain_with("CHANNEL_DELIVERY_UNCERTAIN"),
            "uncertain",
            "CHANNEL_DELIVERY_UNCERTAIN",
        ),
    ):
        env.gateway.test_result = result
        body = env.client.post(
            f"/channels/instances/{instance['id']}/test",
            json={"platform_user_id": "u1"},
            headers=AUTH,
        ).json()
        assert body["certainty"] == certainty
        assert body["error_code"] == code


def test_connectivity_test_on_offline_instance_is_rejected(
    tmp_path, monkeypatch
) -> None:
    """连不上平台时返回 rejected（三态之一），而不是把"实例存在但离线"报成 404。"""
    env = _build(tmp_path, monkeypatch)
    env.gateway.connectable = False
    instance = env.create_instance()

    response = env.client.post(
        f"/channels/instances/{instance['id']}/test",
        json={"platform_user_id": "u1"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["certainty"] == "rejected"
    assert body["error_code"] == "CHANNEL_INSTANCE_OFFLINE"
    assert env.gateway.tests == []


def test_connectivity_test_requires_a_running_gateway(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch, gateway_running=False)
    instance = env.create_instance()
    response = env.client.post(
        f"/channels/instances/{instance['id']}/test",
        json={"platform_user_id": "u1"},
        headers=AUTH,
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CHANNEL_GATEWAY_DISABLED"


def test_connectivity_test_rejects_unknown_conversation_type(
    tmp_path, monkeypatch
) -> None:
    env = _build(tmp_path, monkeypatch)
    instance = env.create_instance()
    response = env.client.post(
        f"/channels/instances/{instance['id']}/test",
        json={"platform_user_id": "u1", "conversation_type": "room"},
        headers=AUTH,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CHANNEL_CREDENTIALS_INVALID"


# ==============================================================================
# 准入策略
# ==============================================================================


def test_access_policy_defaults_to_open(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    instance = env.create_instance()
    body = env.client.get(
        f"/channels/instances/{instance['id']}/access-policy", headers=AUTH
    ).json()
    # 缺失的策略行按默认值返回：缺失即"放开"
    assert body["direct"]["mode"] == "open"
    assert body["direct"]["allowlist"] == []
    assert body["group"]["mode"] == "open"


def test_access_policy_write_validation(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    instance = env.create_instance()
    response = env.client.put(
        f"/channels/instances/{instance['id']}/access-policy",
        json={"direct": {"mode": "closed"}},
        headers=AUTH,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CHANNEL_CREDENTIALS_INVALID"


@pytest.mark.asyncio
async def test_access_policy_takes_effect_immediately(tmp_path, monkeypatch) -> None:
    """写入后立即生效：判定层每次都从库读取，不做进程内快照（框架设计 §9）。"""
    env = _build(tmp_path, monkeypatch)
    instance = env.create_instance(agent_id="agent-1")
    instance_id = instance["id"]
    env.register_router_instance(instance_id, agent_id="agent-1")

    # 先放行一次：此时是默认的 open 策略，会走到回合阶段（agent 不存在 -> 明确错误文案）
    await env.send_inbound(instance_id, "u-denied", event_id="evt-1")
    assert await env.services.channel_router.wait_until_idle(
        Route(instance_id, "direct", "u-denied"), timeout=2.0
    )
    assert cmd.ACCESS_DENIED_TEXT not in env.adapter.texts
    env.adapter.calls.clear()

    updated = env.client.put(
        f"/channels/instances/{instance_id}/access-policy",
        json={"direct": {"mode": "allowlist", "allowlist": ["u-allowed"]}},
        headers=AUTH,
    )
    assert updated.status_code == 200
    assert updated.json()["direct"]["mode"] == "allowlist"

    # 同一个进程内立刻生效：被拒绝的用户收到明确文案，且没有进入队列
    await env.send_inbound(instance_id, "u-denied", event_id="evt-2")
    await env.services.channel_router.wait_until_idle(
        Route(instance_id, "direct", "u-denied"), timeout=2.0
    )
    assert env.adapter.texts == [cmd.ACCESS_DENIED_TEXT]
    assert env.services.channel_router.queue_depth(
        Route(instance_id, "direct", "u-denied")
    ) == 0


def test_access_policy_missing_instance_is_404(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    assert (
        env.client.get("/channels/instances/nope/access-policy", headers=AUTH).status_code
        == 404
    )
