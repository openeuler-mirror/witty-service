"""凭据不泄露的接口级断言（框架设计 §6.2、9.1 第 14 条）。

做法是"金丝雀"：用**不可能自然出现**的字符串作为凭据值，然后遍历所有渠道接口的
响应（含错误响应）与数据库中的原始列，断言它一次都不出现；同时断言掩码只暴露
首 4 位与末 4 位。
"""

from __future__ import annotations

import re

from tests.unit.api.test_channels_api import (
    AUTH,
    TEMP_STATE,
    _build,
)

#: 金丝雀：这些值一旦出现在任何响应里就说明凭据泄露了
CANARY_SECRET = "LEAK-CANARY-2f9a4b7c8d1e-UNIQUE"
CANARY_BOT_ID = "canary-bot-id-7788990011"


def _instance_payload(**overrides: object) -> dict:
    body: dict[str, object] = {
        "channel": "wecom_bot",
        "credentials": {"bot_id": CANARY_BOT_ID, "secret": CANARY_SECRET},
    }
    body.update(overrides)
    return body


def _all_responses(client, instance_id: str) -> list[tuple[str, str]]:
    """所有渠道端点的响应体（含错误响应），覆盖 GET/POST/PATCH/PUT/DELETE。"""
    calls: list[tuple[str, object]] = [
        ("GET", lambda: client.get("/channels/catalog", headers=AUTH)),
        ("GET", lambda: client.get("/channels/instances", headers=AUTH)),
        (
            "GET",
            lambda: client.get(f"/channels/instances/{instance_id}", headers=AUTH),
        ),
        (
            "GET",
            lambda: client.get(
                f"/channels/instances/{instance_id}/access-policy", headers=AUTH
            ),
        ),
        (
            "GET",
            lambda: client.get("/channels/instances/missing", headers=AUTH),
        ),
        (
            "PATCH",
            lambda: client.patch(
                f"/channels/instances/{instance_id}",
                json={"display_name": "改名"},
                headers=AUTH,
            ),
        ),
        (
            "POST",
            lambda: client.post(
                f"/channels/instances/{instance_id}/reconnect", headers=AUTH
            ),
        ),
        (
            "POST",
            lambda: client.post(
                f"/channels/instances/{instance_id}/test",
                json={"platform_user_id": "u1"},
                headers=AUTH,
            ),
        ),
        (
            "PUT",
            lambda: client.put(
                f"/channels/instances/{instance_id}/access-policy",
                json={"direct": {"mode": "open"}},
                headers=AUTH,
            ),
        ),
        (
            "POST",
            lambda: client.post(
                "/channels/instances",
                json={"channel": "wecom_bot", "credentials": {"secret": CANARY_SECRET}},
                headers=AUTH,
            ),
        ),
        (
            "POST",
            lambda: client.post(
                "/channels/instances",
                json={"channel": "nope", "credentials": {"secret": CANARY_SECRET}},
                headers=AUTH,
            ),
        ),
        (
            "POST",
            lambda: client.post(
                "/channels/provision/begin", json={"channel": "wecom_bot"}, headers=AUTH
            ),
        ),
    ]
    return [(name, call().text) for name, call in calls]  # type: ignore[union-attr]


def test_no_response_contains_plaintext_credentials(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    created = env.client.post(
        "/channels/instances", json=_instance_payload(), headers=AUTH
    )
    assert created.status_code == 201, created.text
    instance_id = created.json()["id"]

    for name, body in _all_responses(env.client, instance_id):
        assert CANARY_SECRET not in body, f"{name} response leaked the secret"
        assert TEMP_STATE not in body, f"{name} response leaked the platform temp state"
        # bot_id 由适配器显式声明为**非密配置**（`config_fields`），因此允许出现——
        # 但只允许出现在 `config` 里，不允许混进其它字段（例如掩码或错误详情）。
        outside_config = re.sub(r'"config":\{[^}]*\}', "", body)
        assert CANARY_BOT_ID not in outside_config, (
            f"{name} response leaked bot_id outside the declared config fields"
        )


def test_responses_only_carry_the_declared_mask(tmp_path, monkeypatch) -> None:
    env = _build(tmp_path, monkeypatch)
    created = env.client.post(
        "/channels/instances", json=_instance_payload(), headers=AUTH
    ).json()

    # mask_fields 只声明了 bot_id：掩码是"首 4 + **** + 末 4"
    assert created["credential_mask"] == f"{CANARY_BOT_ID[:4]}****{CANARY_BOT_ID[-4:]}"
    # config 只含适配器显式声明的非密字段，且不含任何密文字段
    assert set(created["config"]) == {"bot_id"}
    assert "secret" not in created["config"]


def test_credentials_never_enter_the_main_database(tmp_path, monkeypatch) -> None:
    """主库里既没有明文，也没有密文：只有一个不含任何凭据信息的不透明引用。"""
    env = _build(tmp_path, monkeypatch)
    created = env.client.post(
        "/channels/instances", json=_instance_payload(), headers=AUTH
    ).json()

    record = env.services.channel_repository.get_instance(created["id"])
    assert record is not None
    assert record.credential_ref is not None
    assert record.credential_ref.startswith("chan_")
    # 引用是实例 id 的摘要，反推不出实例，更推不出凭据
    assert CANARY_SECRET not in record.credential_ref
    assert CANARY_BOT_ID not in record.credential_ref
    assert record.credential_mask is not None
    assert CANARY_SECRET not in str(record.config)

    # 整个库文件里也搜不到那串金丝雀
    database = tmp_path / "channels-api.sqlite3"
    assert CANARY_SECRET.encode() not in database.read_bytes()


def test_credentials_live_in_an_owner_only_file(tmp_path, monkeypatch) -> None:
    """凭据本体在 0600 文件里、目录 0700（目录与文件的权限都必须显式校验）。"""
    env = _build(tmp_path, monkeypatch)
    created = env.client.post(
        "/channels/instances", json=_instance_payload(), headers=AUTH
    ).json()

    store = env.services.get_channel_credentials()
    record = env.services.channel_repository.get_instance(created["id"])
    assert record is not None and record.credential_ref is not None
    # bot_id 由适配器声明为**非密配置**，因此落在 config 列；只有 secret 进凭据文件
    assert store.resolve(record.credential_ref) == {"secret": CANARY_SECRET}

    path = store.directory / f"{record.credential_ref}.json"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (store.directory.stat().st_mode & 0o777) == 0o700


def test_openapi_schema_exposes_no_credential_response_field(
    tmp_path, monkeypatch
) -> None:
    env = _build(tmp_path, monkeypatch)
    schema = env.client.get("/openapi.json").text
    assert "credential_ciphertext" not in schema
    assert "credential_ref" not in schema
    # 请求体允许提交凭据，但响应模型里没有任何 "credentials" 字段
    responses = env.client.get("/openapi.json").json()["components"]["schemas"]
    assert "credentials" not in responses["ChannelInstanceResponse"]["properties"]


def test_insecure_credential_directory_refuses_to_store(tmp_path, monkeypatch) -> None:
    """凭据目录对 group/other 可读时**拒绝写入**：接口给出明确错误码，且不留半成品。"""
    credentials_dir = tmp_path / "leaky-credentials"
    credentials_dir.mkdir(mode=0o755)
    env = _build(tmp_path, monkeypatch, credentials_dir=str(credentials_dir))

    response = env.client.post(
        "/channels/instances", json=_instance_payload(), headers=AUTH
    )

    assert response.status_code == 500
    assert (
        response.json()["error"]["code"] == "CHANNEL_CREDENTIAL_STORE_INSECURE"
    )
    assert env.services.channel_repository.list_instances() == []
    assert list(credentials_dir.iterdir()) == []
