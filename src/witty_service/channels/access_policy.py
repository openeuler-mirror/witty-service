"""准入策略：纯函数模块，无 IO、无副作用（框架设计 §3.7）。

模型上区分两个层级（特性设计文档 6.1）：

1. **对话准入**：这条消息的发送者是否被允许与机器人对话；
2. **命令准入**：被允许对话的人，是否额外被允许执行命令。

MVP 只使用第 1 层且默认放行；第 2 层在数据结构中已建模（`AccessPolicy.allow_commands`）
但不参与判断，留给后续切片。

**fail-closed**：配置缺失、损坏或无法解析时一律拒绝（`decide` 对 `None` 策略返回拒绝）。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

MODE_OPEN = "open"
MODE_ALLOWLIST = "allowlist"
MODES = (MODE_OPEN, MODE_ALLOWLIST)

REASON_MODE_OPEN = "mode_open"
REASON_ALLOWLIST_HIT = "allowlist_hit"
REASON_ALLOWLIST_MISS = "allowlist_miss"
REASON_POLICY_UNAVAILABLE = "policy_unavailable"
REASON_POLICY_INVALID = "policy_invalid"
REASON_EMPTY_ALLOWLIST = "empty_allowlist"


@dataclass(frozen=True, slots=True)
class AccessPolicy:
    """一条会话路由的准入配置（数据库中的业务数据，修改后立即生效）。"""

    mode: str = MODE_OPEN
    allowlist: frozenset[str] = field(default_factory=frozenset)
    #: 命令准入层：MVP 已建模但不参与判断（特性设计文档 6.1）
    allow_commands: bool = True

    @classmethod
    def open(cls) -> AccessPolicy:
        return cls(mode=MODE_OPEN)

    @classmethod
    def allowlist_only(cls, entries: Iterable[str]) -> AccessPolicy:
        return cls(mode=MODE_ALLOWLIST, allowlist=frozenset(entries))


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    reason: str


ALLOW = AccessDecision(allowed=True, reason=REASON_MODE_OPEN)
DENY_UNAVAILABLE = AccessDecision(False, REASON_POLICY_UNAVAILABLE)
DENY_INVALID = AccessDecision(False, REASON_POLICY_INVALID)


def parse_policy(
    mode: object,
    allowlist: object,
    *,
    allow_commands: object = True,
) -> AccessPolicy | None:
    """把数据库中的原始值解析成策略对象；无法解析时返回 None（调用方 fail-closed）。

    `allowlist` 列在库中是 JSON（框架设计 §5.1），既接受已解码的 list，也接受
    JSON 文本；出现任何非字符串元素即视为损坏配置。
    """
    if not isinstance(mode, str):
        return None
    normalized_mode = mode.strip().lower()
    if normalized_mode not in MODES:
        return None
    entries = _parse_allowlist(allowlist)
    if entries is None:
        return None
    if not isinstance(allow_commands, bool):
        return None
    return AccessPolicy(
        mode=normalized_mode,
        allowlist=frozenset(entries),
        allow_commands=allow_commands,
    )


def _parse_allowlist(raw: object) -> list[str] | None:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            decoded: Any = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return _parse_allowlist(decoded)
    if isinstance(raw, (list, tuple, set, frozenset)):
        entries: list[str] = []
        for item in raw:
            if not isinstance(item, str):
                return None
            entries.append(item)
        return entries
    return None


def decide(
    policy: AccessPolicy | None,
    *,
    conversation_type: str,
    platform_user_id: str,
    is_command: bool,
) -> AccessDecision:
    """对话准入判定。

    `conversation_type` 与 `is_command` 参与签名是为了让判定与调用点的语义一致，
    并让"命令准入"在后续切片中可以在不改签名的情况下启用。
    """
    del conversation_type, is_command
    if policy is None:
        return DENY_UNAVAILABLE
    if policy.mode == MODE_OPEN:
        return ALLOW
    if policy.mode == MODE_ALLOWLIST:
        if not policy.allowlist:
            # 白名单模式但没有名单：这不是"放开"，按 fail-closed 拒绝。
            return AccessDecision(False, REASON_EMPTY_ALLOWLIST)
        if platform_user_id in policy.allowlist:
            return AccessDecision(True, REASON_ALLOWLIST_HIT)
        return AccessDecision(False, REASON_ALLOWLIST_MISS)
    return DENY_INVALID


def decide_raw(
    mode: object,
    allowlist: object,
    *,
    conversation_type: str,
    platform_user_id: str,
    is_command: bool,
    allow_commands: object = True,
) -> AccessDecision:
    """从原始库值直接判定：解析失败即拒绝（fail-closed 的唯一入口）。"""
    return decide(
        parse_policy(mode, allowlist, allow_commands=allow_commands),
        conversation_type=conversation_type,
        platform_user_id=platform_user_id,
        is_command=is_command,
    )


__all__ = [
    "MODES",
    "MODE_ALLOWLIST",
    "MODE_OPEN",
    "REASON_ALLOWLIST_HIT",
    "REASON_ALLOWLIST_MISS",
    "REASON_EMPTY_ALLOWLIST",
    "REASON_MODE_OPEN",
    "REASON_POLICY_INVALID",
    "REASON_POLICY_UNAVAILABLE",
    "AccessDecision",
    "AccessPolicy",
    "decide",
    "decide_raw",
    "parse_policy",
]
