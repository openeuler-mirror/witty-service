"""W2 准入策略的表驱动测试（特性设计文档 9.1 第 15 条）。"""

from __future__ import annotations

import pytest

from witty_service.channels.access_policy import (
    MODE_ALLOWLIST,
    MODE_OPEN,
    AccessPolicy,
    decide,
    decide_raw,
    parse_policy,
)

# ==============================================================================
# decide：放开 / 白名单 / fail-closed
# ==============================================================================


@pytest.mark.parametrize("is_command", [False, True])
def test_open_allows(is_command: bool) -> None:
    decision = decide(
        AccessPolicy.open(),
        conversation_type="direct",
        platform_user_id="user-1",
        is_command=is_command,
    )

    assert decision.allowed is True
    assert decision.reason == "mode_open"


def test_allowlist_hit_allows() -> None:
    policy = AccessPolicy.allowlist_only(["user-1", "user-2"])

    decision = decide(
        policy,
        conversation_type="direct",
        platform_user_id="user-2",
        is_command=False,
    )

    assert decision.allowed is True
    assert decision.reason == "allowlist_hit"


@pytest.mark.parametrize("unknown_user", ["user-3", "", "USER-1"])
def test_allowlist_fail_closed(unknown_user: str) -> None:
    """白名单未命中即拒绝（大小写敏感：平台标识原样比对）。"""
    decision = decide(
        AccessPolicy.allowlist_only(["user-1"]),
        conversation_type="direct",
        platform_user_id=unknown_user,
        is_command=False,
    )

    assert decision.allowed is False
    assert decision.reason == "allowlist_miss"


def test_allowlist_mode_without_entries_denies() -> None:
    decision = decide(
        AccessPolicy(mode=MODE_ALLOWLIST),
        conversation_type="direct",
        platform_user_id="user-1",
        is_command=False,
    )

    assert decision.allowed is False
    assert decision.reason == "empty_allowlist"


def test_missing_policy_denies() -> None:
    decision = decide(
        None,
        conversation_type="direct",
        platform_user_id="user-1",
        is_command=False,
    )

    assert decision.allowed is False
    assert decision.reason == "policy_unavailable"


def test_unknown_mode_denies() -> None:
    decision = decide(
        AccessPolicy(mode="whatever"),
        conversation_type="direct",
        platform_user_id="user-1",
        is_command=False,
    )

    assert decision.allowed is False
    assert decision.reason == "policy_invalid"


# ==============================================================================
# parse_policy：损坏配置一律解析失败
# ==============================================================================


def test_parse_policy_accepts_json_text() -> None:
    policy = parse_policy("allowlist", '["user-1", "user-2"]')

    assert policy is not None
    assert policy.mode == MODE_ALLOWLIST
    assert policy.allowlist == frozenset({"user-1", "user-2"})


def test_parse_policy_accepts_decoded_list() -> None:
    policy = parse_policy(" open ", ["user-1"])

    assert policy is not None
    assert policy.mode == MODE_OPEN
    assert policy.allowlist == frozenset({"user-1"})


@pytest.mark.parametrize(
    ("mode", "allowlist"),
    [
        (None, []),
        (123, []),
        ("whatever", []),
        ("open", "not-json"),
        ("open", "[1, 2]"),
        ("open", {"a": 1}),
        ("allowlist", [None]),
    ],
)
def test_parse_policy_rejects_corrupt_config(mode: object, allowlist: object) -> None:
    assert parse_policy(mode, allowlist) is None


@pytest.mark.parametrize(
    ("mode", "allowlist", "expected_allowed"),
    [
        ("open", "[]", True),
        ("allowlist", '["user-1"]', True),
        ("allowlist", '["other"]', False),
        # 损坏配置 -> fail-closed
        ("allowlist", "not-json", False),
        ("broken", "[]", False),
        (None, None, False),
    ],
)
def test_decide_raw_fail_closed(
    mode: object, allowlist: object, expected_allowed: bool
) -> None:
    decision = decide_raw(
        mode,
        allowlist,
        conversation_type="direct",
        platform_user_id="user-1",
        is_command=False,
    )

    assert decision.allowed is expected_allowed


def test_command_layer_is_modeled_but_not_enforced() -> None:
    """命令准入层 MVP 只建模不参与判断（特性设计文档 6.1）。"""
    policy = AccessPolicy.allowlist_only(["user-1"])

    decision = decide(
        policy,
        conversation_type="direct",
        platform_user_id="user-1",
        is_command=True,
    )

    assert decision.allowed is True
    assert policy.allow_commands is True
