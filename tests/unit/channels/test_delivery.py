"""W3 呈现规划的表驱动测试（特性设计文档 9.1 第 9 条）。"""

from __future__ import annotations

import pytest

from witty_service.channels.commands import CONSOLE_NOTICE_TEXT, EMPTY_RESULT_TEXT
from witty_service.channels.contracts import ChannelCapabilities
from witty_service.channels.delivery import (
    PRESENTATION_EDIT_PLACEHOLDER,
    PRESENTATION_SEND_NEW,
    plan,
    split_text,
)

#: 代码围栏标记（避免在测试源码里直接写三个反引号）
FENCE = chr(96) * 3

WECOM = ChannelCapabilities(
    can_edit_message=False, max_text_length=2000, max_reply_segments=None
)
QQ = ChannelCapabilities(
    can_edit_message=False, max_text_length=4500, max_reply_segments=4
)
FEISHU = ChannelCapabilities(
    can_edit_message=True, max_text_length=2000, max_reply_segments=None
)


def _assert_within_limit(segments: list[str], max_length: int) -> None:
    for segment in segments:
        assert len(segment) <= max_length, repr(segment)


# ==============================================================================
# 呈现方式
# ==============================================================================


def test_presentation_follows_capability() -> None:
    assert plan("hi", FEISHU).presentation == PRESENTATION_EDIT_PLACEHOLDER
    assert plan("hi", WECOM).presentation == PRESENTATION_SEND_NEW


def test_short_text_is_single_action() -> None:
    result = plan("结果是 42", WECOM)

    assert len(result.actions) == 1
    assert result.actions[0].index == 0
    assert result.actions[0].text == "结果是 42"
    assert result.actions[0].is_console_notice is False
    assert result.truncated is False


@pytest.mark.parametrize("text", ["", "   ", "\n\n", None])
def test_empty_text_yields_fallback(text: str | None) -> None:
    result = plan(text, WECOM)

    assert [action.text for action in result.actions] == [EMPTY_RESULT_TEXT]


def test_invalid_max_text_length_falls_back_to_default() -> None:
    result = plan("x" * 3000, ChannelCapabilities(True, 0, None))

    assert len(result.actions) > 1
    _assert_within_limit([action.text for action in result.actions], 2000)


# ==============================================================================
# 切点优先级
# ==============================================================================


def test_splits_at_paragraph_first() -> None:
    text = "第一段内容。\n\n第二段内容。"
    segments = split_text(text, max_length=12)

    assert segments == ["第一段内容。", "第二段内容。"]


def test_splits_at_newline_before_hard_cut() -> None:
    text = "第一行内容\n第二行内容"
    segments = split_text(text, max_length=8)

    assert segments == ["第一行内容", "第二行内容"]


def test_splits_at_sentence_mark() -> None:
    text = "句子一。句子二。句子三。"
    segments = split_text(text, max_length=7)

    assert segments[0] == "句子一。"
    _assert_within_limit(segments, 7)


def test_hard_cut_when_no_separator() -> None:
    segments = split_text("a" * 25, max_length=10)

    assert segments == ["a" * 10, "a" * 10, "a" * 5]


def test_long_paragraph_never_exceeds_limit() -> None:
    text = "这是一个很长的句子。" * 40
    segments = split_text(text, max_length=50)

    _assert_within_limit(segments, 50)
    assert "".join(segments).replace("\n", "") == text.replace("\n", "")


# ==============================================================================
# 结构感知：代码围栏与表格
# ==============================================================================


def test_no_split_inside_code_fence() -> None:
    """代码围栏能装进一条消息时，绝不被拆开（9.1 第 9 条）。"""
    code = "\n".join([f"{FENCE}python", *[f"print({i})" for i in range(20)], FENCE])
    text = f"说明段落。\n\n{code}\n\n结尾段落。"

    # 上限刚好装得下代码块、但装不下"代码块 + 相邻段落"，用于逼出切分点
    segments = split_text(text, max_length=205)

    assert len(segments) == 3
    _assert_within_limit(segments, 205)
    holder = [segment for segment in segments if "print(0)" in segment]
    assert len(holder) == 1
    # 整块代码在同一个段里
    assert "print(19)" in holder[0]
    assert holder[0].splitlines()[0].endswith(f"{FENCE}python")
    assert holder[0].splitlines()[-1] == FENCE


def test_oversized_code_fence_is_reopened_not_cut() -> None:
    """超长围栏按行重新成块：每一段都自带开闭围栏，不出现半截围栏。"""
    code = "\n".join([FENCE, *[f"line{i}" for i in range(40)], FENCE])

    segments = split_text(code, max_length=60)

    assert len(segments) > 1
    _assert_within_limit(segments, 60)
    for segment in segments:
        lines = segment.split("\n")
        assert lines[0] == FENCE
        assert lines[-1] == FENCE
        assert all(line.startswith("line") for line in lines[1:-1])


def test_fences_stay_balanced_in_every_segment() -> None:
    text = "前言。\n\n" + "\n".join([FENCE, *[f"code{i}" for i in range(30)], FENCE]) + "\n\n后记。"
    segments = split_text(text, max_length=80)

    for segment in segments:
        assert segment.count(FENCE) % 2 == 0


def test_table_rows_are_not_cut() -> None:
    """表格按行拆分并重复表头，不切断表格（9.1 第 9 条）。"""
    header = "| id | name |\n| --- | --- |"
    rows = "\n".join(f"| {i} | n{i} |" for i in range(30))
    text = f"{header}\n{rows}"

    segments = split_text(text, max_length=100)

    assert len(segments) > 1
    _assert_within_limit(segments, 100)
    for segment in segments:
        lines = segment.split("\n")
        assert lines[0] == "| id | name |"
        assert lines[1] == "| --- | --- |"
        assert all(line.startswith("|") and line.endswith("|") for line in lines[2:])


def test_small_table_kept_intact() -> None:
    text = "| id | name |\n| --- | --- |\n| 1 | a |"
    segments = split_text(text, max_length=200)

    assert segments == [text]


# ==============================================================================
# 条数上限：QQ 4 条 + 提示条
# ==============================================================================


def test_qq_caps_at_four_segments() -> None:
    # 每段约 3000 字符：各自独占一条，条数明显超过 4 条上限
    text = "\n\n".join(f"段落{i}。" + "内容" * 1500 for i in range(12))

    result = plan(text, QQ)

    assert len(result.actions) == 4
    assert result.truncated is True
    assert result.actions[-1].text == CONSOLE_NOTICE_TEXT
    assert result.actions[-1].is_console_notice is True
    assert all(not action.is_console_notice for action in result.actions[:-1])
    _assert_within_limit([action.text for action in result.actions], 4500)


def test_unlimited_segments_are_not_truncated() -> None:
    text = "\n\n".join(f"段落{i}。" + "内容" * 500 for i in range(8))

    result = plan(text, WECOM)

    assert result.truncated is False
    assert all(not action.is_console_notice for action in result.actions)
    assert CONSOLE_NOTICE_TEXT not in [action.text for action in result.actions]


def test_single_segment_limit_keeps_only_notice() -> None:
    text = "\n\n".join("内容" * 200 for _ in range(6))
    caps = ChannelCapabilities(False, 100, 1)

    result = plan(text, caps)

    assert [action.text for action in result.actions] == [CONSOLE_NOTICE_TEXT]
    assert result.truncated is True


def test_actions_are_indexed_in_order() -> None:
    text = "\n\n".join(f"段落{i}。" + "内容" * 1500 for i in range(12))

    result = plan(text, QQ)

    assert [action.index for action in result.actions] == [0, 1, 2, 3]
