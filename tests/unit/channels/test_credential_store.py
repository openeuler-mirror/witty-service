"""渠道凭据存储的测试：权限纪律、原子写、损坏与非法引用的拒绝路径。

这一组测试同时充当"凭据不再落库"的回归网：任何一个"图省事把凭据写回数据库"的
改动都会先在这里失败。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from witty_service.channels import errors as err
from witty_service.channels.credential_store import (
    CREDENTIAL_FILE_VERSION,
    ChannelCredentialStore,
)
from witty_service.domain.errors import DomainError

INSTANCE_ID = "77c19891-eb46-4006-b5aa-bc6fd4cac3b6"


@pytest.fixture
def store(tmp_path) -> ChannelCredentialStore:
    return ChannelCredentialStore(tmp_path / "channel-credentials")


def _write_raw(store: ChannelCredentialStore, ref: str, text: str) -> None:
    """按"内容正确、权限也正确"的方式手工放一个文件：权限校验先于内容解析，
    因此这些用例必须先把 0600 设好，否则测到的是权限路径而不是解析路径。"""
    store.ensure_ready()
    path = store.directory / f"{ref}.json"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


# ==============================================================================
# 引用
# ==============================================================================


def test_ref_is_stable_opaque_and_namespaced(store: ChannelCredentialStore) -> None:
    ref = store.ref_for_instance(INSTANCE_ID)

    assert ref == store.ref_for_instance(INSTANCE_ID)
    assert ref.startswith("chan_")
    assert ref != store.ref_for_instance("another-instance")
    # 引用反推不出实例 id，也不含凭据信息——它要落在数据库里
    assert INSTANCE_ID not in ref
    assert store.ref_for_provisioning("attempt-1").startswith("prov_")


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "chan_short",
        "chan_zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
        "zzz_" + "a" * 32,
        "../../etc/passwd",
        "chan_" + "a" * 32 + "/../../x",
    ],
)
def test_invalid_reference_is_refused(store: ChannelCredentialStore, ref: str) -> None:
    """引用会被拼进文件路径，因此必须**严格**校验（否则就是目录穿越）。"""
    with pytest.raises(DomainError) as excinfo:
        store.resolve(ref)
    assert excinfo.value.code == err.CHANNEL_CREDENTIALS_INVALID


# ==============================================================================
# 读写与权限
# ==============================================================================


def test_write_creates_the_directory_and_is_owner_only(
    store: ChannelCredentialStore,
) -> None:
    ref = store.ref_for_instance(INSTANCE_ID)

    entry = store.write(ref, {"secret": "s3cr3t"})

    assert entry.values == {"secret": "s3cr3t"}
    assert entry.kind == "instance"
    assert (store.directory.stat().st_mode & 0o777) == 0o700
    path = store.directory / f"{ref}.json"
    assert (path.stat().st_mode & 0o777) == 0o600
    assert store.resolve(ref) == {"secret": "s3cr3t"}


@pytest.mark.parametrize("directory", ["", "   ", "relative/credentials", "./creds"])
def test_relative_or_empty_directory_is_refused(directory: str) -> None:
    """相对路径会把"凭据放哪儿"交给工作目录：可能写进仓库，也可能随启动方式漂移。"""
    with pytest.raises(DomainError) as excinfo:
        ChannelCredentialStore(directory)

    assert excinfo.value.code == err.CHANNEL_CREDENTIAL_STORE_UNAVAILABLE


def test_missing_reference_resolves_to_none(store: ChannelCredentialStore) -> None:
    assert store.resolve(store.ref_for_instance("never-written")) is None


def test_write_leaves_no_temporary_files(store: ChannelCredentialStore) -> None:
    ref = store.ref_for_instance(INSTANCE_ID)

    store.write(ref, {"secret": "one"})
    store.write(ref, {"secret": "two"})

    assert sorted(p.name for p in store.directory.iterdir()) == [f"{ref}.json"]
    assert store.resolve(ref) == {"secret": "two"}


def test_overwrite_keeps_created_at_and_refreshes_values(
    store: ChannelCredentialStore,
) -> None:
    ref = store.ref_for_instance(INSTANCE_ID)
    first = store.write(ref, {"secret": "one"})

    second = store.write(ref, {"secret": "two"})

    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at


def test_delete_is_idempotent(store: ChannelCredentialStore) -> None:
    ref = store.ref_for_instance(INSTANCE_ID)
    store.write(ref, {"secret": "s"})

    assert store.delete(ref) is True
    assert store.delete(ref) is False
    assert store.resolve(ref) is None


def test_group_readable_file_is_refused(store: ChannelCredentialStore) -> None:
    """世界可读的凭据文件已经等同于泄露：拒绝使用，并在错误里给出修复命令。"""
    ref = store.ref_for_instance(INSTANCE_ID)
    store.write(ref, {"secret": "s3cr3t"})
    path = store.directory / f"{ref}.json"
    path.chmod(0o640)

    with pytest.raises(DomainError) as excinfo:
        store.resolve(ref)

    assert excinfo.value.code == err.CHANNEL_CREDENTIAL_STORE_INSECURE
    assert f"chmod 0600 {path}" in str(excinfo.value.details["reason"])


def test_group_readable_directory_is_refused(store: ChannelCredentialStore) -> None:
    store.ensure_ready()
    store.directory.chmod(0o755)

    with pytest.raises(DomainError) as excinfo:
        store.ensure_ready()

    assert excinfo.value.code == err.CHANNEL_CREDENTIAL_STORE_INSECURE
    assert excinfo.value.details["kind"] == "directory"


def test_write_refuses_an_insecure_directory(store: ChannelCredentialStore) -> None:
    store.ensure_ready()
    store.directory.chmod(0o755)

    with pytest.raises(DomainError) as excinfo:
        store.write(store.ref_for_instance(INSTANCE_ID), {"secret": "s"})

    assert excinfo.value.code == err.CHANNEL_CREDENTIAL_STORE_INSECURE
    assert list(store.directory.iterdir()) == []


# ==============================================================================
# 损坏的文件
# ==============================================================================


def test_corrupt_json_is_refused(store: ChannelCredentialStore) -> None:
    ref = store.ref_for_instance(INSTANCE_ID)
    _write_raw(store, ref, "{not json")

    with pytest.raises(DomainError) as excinfo:
        store.resolve(ref)

    assert excinfo.value.code == err.CHANNEL_CREDENTIALS_INVALID


def test_unsupported_version_is_refused(store: ChannelCredentialStore) -> None:
    ref = store.ref_for_instance(INSTANCE_ID)
    _write_raw(
        store,
        ref,
        json.dumps(
            {"version": CREDENTIAL_FILE_VERSION + 1, "values": {"secret": "s"}}
        ),
    )

    with pytest.raises(DomainError) as excinfo:
        store.resolve(ref)

    assert excinfo.value.code == err.CHANNEL_CREDENTIALS_INVALID
    assert "unsupported version" in str(excinfo.value.details["reason"])


def test_non_object_values_are_refused(store: ChannelCredentialStore) -> None:
    ref = store.ref_for_instance(INSTANCE_ID)
    _write_raw(
        store,
        ref,
        json.dumps({"version": CREDENTIAL_FILE_VERSION, "values": ["nope"]}),
    )

    with pytest.raises(DomainError) as excinfo:
        store.resolve(ref)

    assert excinfo.value.code == err.CHANNEL_CREDENTIALS_INVALID


# ==============================================================================
# 回收
# ==============================================================================


def test_prune_expired_removes_only_expired_provisioning_entries(
    store: ChannelCredentialStore,
) -> None:
    now = datetime.now(UTC)
    expired = store.ref_for_provisioning("attempt-expired")
    live = store.ref_for_provisioning("attempt-live")
    instance_ref = store.ref_for_instance(INSTANCE_ID)
    store.write(expired, {"state_b64": "eA=="}, expires_at=now - timedelta(minutes=1))
    store.write(live, {"state_b64": "eA=="}, expires_at=now + timedelta(minutes=5))
    store.write(instance_ref, {"secret": "s"})

    removed = store.prune_expired(now=now)

    assert removed == 1
    assert store.resolve(expired) is None
    assert store.resolve(live) == {"state_b64": "eA=="}
    # 实例凭据永远不在这里被回收：它们没有过期时间
    assert store.resolve(instance_ref) == {"secret": "s"}


def test_prune_expired_tolerates_a_corrupt_file(store: ChannelCredentialStore) -> None:
    """读不懂的文件宁可留着——删掉一个读不懂的文件是在赌它不是唯一的凭据。"""
    ref = store.ref_for_provisioning("attempt-corrupt")
    _write_raw(store, ref, "{oops")

    assert store.prune_expired() == 0
    assert (store.directory / f"{ref}.json").exists()


def test_prune_expired_on_a_missing_directory_is_a_no_op(
    store: ChannelCredentialStore,
) -> None:
    assert store.prune_expired() == 0
