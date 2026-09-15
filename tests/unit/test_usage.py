"""``session.usage`` 载荷归一化与聚合的单测。

只覆盖各 runtime 单测拿不到的形态：真实上游载荷、历史存量行、歧义/脏数据。
"""

from __future__ import annotations

from typing import Any

import pytest

from witty_agent_server.runtimes.usage import (
    has_token_usage,
    merge_usage,
    normalize_usage_payload,
)

# ---------------------------------------------------------------------------
# 三个上游形态 → 统一契约载荷（形态取自真实事件）
# ---------------------------------------------------------------------------


def test_normalize_dsh_payload_derives_total() -> None:
    """dsh：mapUsage 结果与 assistant/message 同级，camelCase，且不给总量。"""
    payload = {
        "usage": {
            "inputTokens": 2171,
            "outputTokens": 36,
            "cacheReadTokens": 0,
            "reasoningTokens": 34,
        }
    }

    assert normalize_usage_payload(payload) == {
        "input_tokens": 2171,
        "output_tokens": 36,
        "cache_read_tokens": 0,
        "reasoning_tokens": 34,
        # dsh 的 inputTokens 已扣除 cacheRead，两者互不重叠：2171 + 36 + 0
        "total_tokens": 2207,
    }


def test_normalize_opencode_payload() -> None:
    """opencode：input/output/reasoning/total + cache{read,write}，cost 在同级。"""
    payload = {
        "type": "step-finish",
        "usage": {
            "input": 5901,
            "output": 243,
            "reasoning": 116,
            "cache": {"read": 1920, "write": 0},
            "total": 8180,
        },
        "cost": 0.000932036,
    }

    assert normalize_usage_payload(payload) == {
        "input_tokens": 5901,
        "output_tokens": 243,
        "cache_read_tokens": 1920,
        "cache_write_tokens": 0,
        "reasoning_tokens": 116,
        "total_tokens": 8180,
        "total_cost": 0.000932036,
    }


def test_normalize_openclaw_step_usage_with_cost_object() -> None:
    """openclaw：成本是 ``cost`` 子对象，取其中的 total 而不是把对象当数字。

    同时确认 ``cost.cacheRead``（金额）不会被当成 cache_read_tokens 计数。
    """
    payload = {
        "input": 215,
        "output": 27,
        "cacheRead": 19712,
        "cacheWrite": 0,
        "totalTokens": 19954,
        "cost": {
            "input": 3.01e-05,
            "output": 7.56e-06,
            "cacheRead": 0.000551936,
            "cacheWrite": 0,
            "total": 0.000589596,
        },
    }

    assert normalize_usage_payload(payload) == {
        "input_tokens": 215,
        "output_tokens": 27,
        "cache_read_tokens": 19712,
        "cache_write_tokens": 0,
        "total_tokens": 19954,
        "total_cost": 0.000589596,
    }


def test_normalize_upstream_total_wins_over_derived() -> None:
    payload = {"usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 99}}

    assert normalize_usage_payload(payload)["total_tokens"] == 99


@pytest.mark.parametrize(
    "raw",
    [
        # 历史存量：openclaw 会话累计快照（sessions.usage 返回值）与老 REST 契约
        {"data": {"sessions": [{"usage": {"inputTokens": 4, "outputTokens": 6}}]}},
        {"tokens": {"input": 7, "output": 3, "total": 10}},
        {"input_tokens": 10, "output_tokens": 5, "total_cost": 0.1},
    ],
)
def test_normalize_legacy_payload_shapes(raw: dict[str, Any]) -> None:
    assert normalize_usage_payload(raw)


def test_normalize_is_idempotent_for_contract_payload() -> None:
    contract = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_tokens": 1,
        "reasoning_tokens": 2,
        "total_tokens": 16,
        "total_cost": 0.1,
    }

    assert normalize_usage_payload(contract) == contract


@pytest.mark.parametrize(
    "raw",
    [None, {}, "usage", 3, 3.5, [], True, {"other": 1}, {"usage": {}}],
)
def test_normalize_returns_empty_for_unusable_payload(raw: Any) -> None:
    assert normalize_usage_payload(raw) == {}


def test_normalize_ignores_boolean_counters() -> None:
    """bool 是 int 子类：True 不能被当成 1 计入用量。"""
    assert normalize_usage_payload({"inputTokens": True, "outputTokens": 2}) == {
        "output_tokens": 2,
        "total_tokens": 2,
    }


# ---------------------------------------------------------------------------
# has_token_usage / merge_usage
# ---------------------------------------------------------------------------


def test_has_token_usage_requires_token_counter() -> None:
    assert has_token_usage({"input_tokens": 1}) is True
    # 0 也是有效计数（例如 cacheReadTokens=0），只有 cost 的载荷不算用量事件
    assert has_token_usage({"input_tokens": 0}) is True
    assert has_token_usage({"total_cost": 0.1}) is False
    # openclaw 的 cost 子对象同样只有成本、没有 token 计数
    assert has_token_usage(normalize_usage_payload({"cost": {"total": 0.1}})) is False
    assert has_token_usage({}) is False


def test_merge_usage_sums_counters_and_cost() -> None:
    merged = merge_usage(
        {"input_tokens": 10, "total_tokens": 15, "total_cost": 0.25},
        {
            "input_tokens": 20,
            "cache_read_tokens": 30,
            "total_tokens": 50,
            "total_cost": 0.5,
        },
    )

    assert merged == {
        "input_tokens": 30,
        "cache_read_tokens": 30,
        "total_tokens": 65,
        "total_cost": 0.75,
    }
