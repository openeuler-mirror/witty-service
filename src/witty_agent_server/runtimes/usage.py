"""``session.usage`` 事件载荷的归一化与单轮聚合工具。

载荷契约
--------
``payload`` 是**扁平 snake_case** 的用量增量对象（``input_tokens`` /
``output_tokens`` / ``cache_read_tokens`` / ``cache_write_tokens`` /
``reasoning_tokens`` / ``total_tokens`` / ``total_cost``）。

语义与口径
----------
- 表示**本轮**（跨 step 累计）增量，不是会话累计值；每轮只在终止事件之前下发
  一条（见 ``RuntimeBase.run_turn``）。
- ``input_tokens`` 与 ``cache_*_tokens`` 互不重叠（dsh 口径）：
  ``total_tokens = input + cache_read + cache_write + output``。
- ``reasoning_tokens`` 是否已计入 ``output_tokens`` 取决于上游口径
  （dsh 计入、opencode 单列），原样透传，仅用于展示细分。
- 上游给出总量时沿用；缺失时按上面的口径派生。

三个 runtime 的上游形态各不相同（dsh camelCase、opencode
``input/output/cache{read,write}/total`` + 同级 ``cost`` 标量、openclaw assistant
message 上的 ``input/output/cacheRead/cacheWrite/totalTokens`` + ``cost{...}``
子对象），统一经 ``normalize_usage_payload`` 落到同一契约。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_COUNT_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)

# 除 total_tokens 外的计数；reasoning 是 output 的细分，不参与 total 派生。
_COUNTER_KEYS: tuple[str, ...] = (*_COUNT_FIELDS, "reasoning_tokens", "total_tokens")

# 三种已见过的计数写法：snake_case / OpenAI 的 prompt|completion / dsh camelCase /
# opencode 与 openclaw 的简写（``input``/``output``/``cacheRead``/``total``）。
_ALIASES: dict[str, tuple[str, ...]] = {
    "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "input"),
    "output_tokens": ("output_tokens", "outputTokens", "completion_tokens", "output"),
    "cache_read_tokens": (
        "cache_read_tokens",
        "cacheReadTokens",
        "cache_read_input_tokens",
        "cacheRead",
        "cache_read",
    ),
    "cache_write_tokens": (
        "cache_write_tokens",
        "cacheWriteTokens",
        "cache_creation_input_tokens",
        "cacheWrite",
        "cache_write",
    ),
    "reasoning_tokens": (
        "reasoning_tokens",
        "reasoningTokens",
        "reasoning_output_tokens",
        "reasoning",
    ),
    "total_tokens": ("total_tokens", "totalTokens", "total"),
    # ``cost`` 本身可能不是数字（openclaw 的 cost 子对象），由 _is_number 过滤。
    "total_cost": (
        "total_cost",
        "totalCost",
        "estimatedCostUsd",
        "estimated_cost_usd",
        "cost",
    ),
}

# cache/cost 容器的子层只认本容器的简写键（opencode 的 ``cache.read``、
# openclaw 的 ``cost.total``），否则 ``cache.read`` 会被当成主输入。
_CACHE_ALIASES: dict[str, tuple[str, ...]] = {
    "cache_read_tokens": ("read", "read_tokens", "readTokens"),
    "cache_write_tokens": ("write", "write_tokens", "writeTokens"),
}
_COST_ALIASES: dict[str, tuple[str, ...]] = {
    "total_cost": ("total", "total_cost", "totalCost", "cost"),
}

# 容器键 → 子层别名表；未列出的键（usage/tokens/data/sessions 等包裹层）沿用
# 通用表，因为它们的子层仍可能是 cache/cost 容器。
_CONTAINER_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "cache": _CACHE_ALIASES,
    "cost": _COST_ALIASES,
}

_CONTAINER_KEYS: tuple[str, ...] = (
    "usage",
    "tokens",
    "cache",
    "cost",
    "data",
    "sessions",
)


def normalize_usage_payload(raw: Any) -> dict[str, Any]:
    """把任一上游用量载荷归一化为契约载荷（扁平 snake_case）。

    识别不出任何用量字段时返回空字典，调用方可据此跳过该事件；已是契约载荷的
    入参幂等通过。
    """
    if not isinstance(raw, Mapping):
        return {}

    result: dict[str, Any] = {}
    # 广度优先：先取本级字段（opencode 的 cost 与 usage 同级），再逐层下钻容器；
    # 第一个命中的来源生效，避免多层同名数字被重复累加。
    queue: list[tuple[Mapping[str, Any], dict[str, tuple[str, ...]]]] = [
        (raw, _ALIASES)
    ]
    seen: set[int] = set()
    while queue:
        current, aliases_by_field = queue.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))

        for field, aliases in aliases_by_field.items():
            if field in result:
                continue
            value = _pick(current, aliases)
            if value is not None:
                result[field] = value

        for key in _CONTAINER_KEYS:
            child_aliases = _CONTAINER_ALIASES.get(key, _ALIASES)
            queue.extend((item, child_aliases) for item in _children(current.get(key)))

    if "total_tokens" not in result:
        parts = [result[field] for field in _COUNT_FIELDS if field in result]
        if parts:
            result["total_tokens"] = sum(parts)
    return result


def has_token_usage(usage: Mapping[str, Any]) -> bool:
    """判断载荷是否带有 token 计数（只有 cost 的载荷不构成有效用量事件）。"""
    return any(usage.get(field) is not None for field in _COUNTER_KEYS)


def merge_usage(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """把两段用量载荷相加（单轮内跨 step 累计）。"""
    merged = dict(base)
    for key, value in extra.items():
        current = merged.get(key)
        merged[key] = (
            current + value if _is_number(current) and _is_number(value) else value
        )
    return merged


def _pick(payload: Mapping[str, Any], aliases: tuple[str, ...]) -> int | float | None:
    for key in aliases:
        value = payload.get(key)
        if _is_number(value):
            return value
    return None


def _children(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _is_number(value: Any) -> bool:
    """bool 是 int 子类：True 不能被当成 1 计入用量。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool)
