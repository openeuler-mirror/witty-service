"""呈现规划：纯函数模块，不做任何 IO（框架设计 §3.8）。

输入"一段最终文本 + 能力声明"，输出"**呈现方式**（原地更新 / 补发新消息）与
**若干次发送动作**"。

分段规则：

- 按 `max_text_length` 切分，切点优先级 **段落 → 换行 → 句号 → 空格 → 硬切**；
- **结构感知**：代码围栏与表格都是不可在中间切断的结构块；超长的围栏按行重新
  成块（每块补齐围栏标记），超长的表格按行拆并重复表头，因此不会出现"半截围栏"
  或"半截表格"；
- 超过 `max_reply_segments` 时，**保留最后一条**用于告知"完整结果请在控制台查看"；
- 呈现方式由 `can_edit_message` 决定：可原地编辑的渠道更新占位消息，其余补发新消息。
"""

from __future__ import annotations

from dataclasses import dataclass

from witty_service.channels.commands import CONSOLE_NOTICE_TEXT, EMPTY_RESULT_TEXT
from witty_service.channels.contracts import ChannelCapabilities

#: `max_text_length` 非法（<=0）时使用的保守上限
DEFAULT_MAX_TEXT_LENGTH = 2000

PRESENTATION_EDIT_PLACEHOLDER = "edit_placeholder"
PRESENTATION_SEND_NEW = "send_new"

FENCE = "```"
TABLE_ROW_PREFIX = "|"

_BLOCK_PARAGRAPH = "paragraph"
_BLOCK_CODE = "code"
_BLOCK_TABLE = "table"

#: 切点优先级：(候选分隔串, 切点相对分隔串末尾的偏移)。顺序即优先级。
_CUT_PRIORITIES: tuple[tuple[tuple[str, ...], int], ...] = (
    (("\n\n",), 2),
    (("\n",), 1),
    (("。", "！", "？"), 1),
    (("! ", "? ", ". "), 2),
    ((" ",), 1),
)


@dataclass(frozen=True, slots=True)
class SendAction:
    """一次出站发送动作。`segment_index` 即列表下标，用于投递记录与排障。"""

    index: int
    text: str
    #: 是否为"完整结果请在控制台查看"的提示条（被条数上限截断时才有）
    is_console_notice: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    presentation: str
    actions: tuple[SendAction, ...]
    #: 是否因 `max_reply_segments` 丢弃了内容（排障与日志用）
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class _Block:
    kind: str
    text: str


def plan(text: str | None, capabilities: ChannelCapabilities) -> DeliveryPlan:
    """把最终文本换算成呈现方式与发送动作。"""
    max_length = (
        capabilities.max_text_length
        if capabilities.max_text_length > 0
        else DEFAULT_MAX_TEXT_LENGTH
    )
    presentation = (
        PRESENTATION_EDIT_PLACEHOLDER
        if capabilities.can_edit_message
        else PRESENTATION_SEND_NEW
    )
    content = text if isinstance(text, str) and text.strip() else EMPTY_RESULT_TEXT
    segments = split_text(content, max_length=max_length)
    if not segments:
        segments = [EMPTY_RESULT_TEXT]

    truncated = False
    limit = capabilities.max_reply_segments
    if limit is not None:
        normalized_limit = max(1, limit)
        if len(segments) > normalized_limit:
            truncated = True
            if normalized_limit == 1:
                # 只允许一条：提示条单独成条，内容只能在控制台查看。
                segments = [CONSOLE_NOTICE_TEXT]
            else:
                segments = [*segments[: normalized_limit - 1], CONSOLE_NOTICE_TEXT]

    actions = tuple(
        SendAction(
            index=index,
            text=segment,
            is_console_notice=truncated and index == len(segments) - 1,
        )
        for index, segment in enumerate(segments)
    )
    return DeliveryPlan(
        presentation=presentation, actions=actions, truncated=truncated
    )


def split_text(text: str, *, max_length: int) -> list[str]:
    """结构感知分段：每段长度不超过 `max_length`（不可再切的单行除外）。"""
    if max_length <= 0:
        max_length = DEFAULT_MAX_TEXT_LENGTH
    if not text.strip():
        return []
    segments: list[str] = []
    current = ""
    for block in _iter_blocks(text):
        for chunk in _split_block(block, max_length):
            candidate = f"{current}\n\n{chunk}" if current else chunk
            if len(candidate) <= max_length:
                current = candidate
                continue
            if current:
                segments.append(current)
            current = chunk
    if current:
        segments.append(current)
    return segments


# ==============================================================================
# 结构切块
# ==============================================================================


def _iter_blocks(text: str) -> list[_Block]:
    """把文本切成结构块：围栏代码块 / 表格 / 普通段落。"""
    lines = text.split("\n")
    blocks: list[_Block] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            joined = "\n".join(paragraph).strip("\n")
            if joined.strip():
                blocks.append(_Block(_BLOCK_PARAGRAPH, joined))
            paragraph.clear()

    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith(FENCE):
            flush_paragraph()
            start = index
            index += 1
            while index < len(lines) and not lines[index].strip().startswith(FENCE):
                index += 1
            if index < len(lines):
                index += 1  # 闭合围栏
            blocks.append(_Block(_BLOCK_CODE, "\n".join(lines[start:index])))
            continue
        if stripped.startswith(TABLE_ROW_PREFIX):
            flush_paragraph()
            start = index
            while index < len(lines) and lines[index].strip().startswith(
                TABLE_ROW_PREFIX
            ):
                index += 1
            blocks.append(_Block(_BLOCK_TABLE, "\n".join(lines[start:index])))
            continue
        if not stripped:
            flush_paragraph()
            index += 1
            continue
        paragraph.append(line)
        index += 1
    flush_paragraph()
    return blocks


def _split_block(block: _Block, max_length: int) -> list[str]:
    if len(block.text) <= max_length:
        return [block.text]
    if block.kind == _BLOCK_CODE:
        return _split_code(block.text, max_length)
    if block.kind == _BLOCK_TABLE:
        return _split_table(block.text, max_length)
    return _split_paragraph(block.text, max_length)


def _split_code(text: str, max_length: int) -> list[str]:
    """超长围栏按行重新成块：每块都自带开闭围栏，绝不出现半截围栏。"""
    lines = text.split("\n")
    opening = lines[0]
    closing = FENCE
    body = lines[1:]
    if body and body[-1].strip().startswith(FENCE):
        body = body[:-1]
    budget = max(1, max_length - len(opening) - len(closing) - 2)
    return [
        f"{opening}\n{group}\n{closing}" for group in _group_lines(body, budget)
    ]


def _split_table(text: str, max_length: int) -> list[str]:
    """超长表格按行拆并重复表头，绝不出现半截表格行。"""
    lines = text.split("\n")
    header = lines[0]
    body_start = 1
    prefix = [header]
    if len(lines) > 1 and _is_table_separator(lines[1]):
        prefix.append(lines[1])
        body_start = 2
    body = lines[body_start:]
    overhead = len("\n".join(prefix)) + 1
    budget = max(1, max_length - overhead)
    segments: list[str] = []
    for group in _group_lines(body, budget):
        segments.append("\n".join([*prefix, *group.split("\n")]))
    return segments


def _is_table_separator(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith(TABLE_ROW_PREFIX):
        return False
    return all(char in "|-: \t" for char in stripped)


def _group_lines(lines: list[str], budget: int) -> list[str]:
    """把若干行打包成不超过 `budget` 的块；单行超预算时按字符硬切。"""
    groups: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in lines:
        if len(line) > budget:
            if current:
                groups.append("\n".join(current))
                current = []
                current_length = 0
            groups.extend(_hard_split(line, budget))
            continue
        addition = len(line) + (1 if current else 0)
        if current and current_length + addition > budget:
            groups.append("\n".join(current))
            current = [line]
            current_length = len(line)
            continue
        current.append(line)
        current_length += addition
    if current:
        groups.append("\n".join(current))
    return groups


def _hard_split(text: str, budget: int) -> list[str]:
    return [text[index : index + budget] for index in range(0, len(text), budget)]


# ==============================================================================
# 普通段落切分
# ==============================================================================


def _split_paragraph(text: str, max_length: int) -> list[str]:
    segments: list[str] = []
    remaining = text
    while len(remaining) > max_length:
        cut = _find_cut_point(remaining, max_length)
        head = remaining[:cut].rstrip()
        if not head:
            cut = max_length
            head = remaining[:cut].rstrip()
        if head:
            segments.append(head)
        remaining = remaining[cut:].lstrip("\n")
    if remaining.strip():
        segments.append(remaining)
    return segments


def _find_cut_point(text: str, limit: int) -> int:
    """在 `limit` 以内按优先级找切点；找不到合适的切点则硬切。

    优先保证"结构优先级"，其次保证"段不会过短"：若某优先级只有小于
    `limit // 2` 的切点，先记下来，继续尝试更低优先级；都没有才用它。
    """
    minimum = max(1, limit // 2)
    window = text[:limit]
    fallback = 0
    for needles, offset in _CUT_PRIORITIES:
        position = -1
        for needle in needles:
            found = window.rfind(needle)
            if found >= 0:
                position = max(position, found + offset)
        if position <= 0:
            continue
        if position >= minimum:
            return position
        fallback = max(fallback, position)
    return fallback if fallback > 0 else limit


__all__ = [
    "DEFAULT_MAX_TEXT_LENGTH",
    "PRESENTATION_EDIT_PLACEHOLDER",
    "PRESENTATION_SEND_NEW",
    "DeliveryPlan",
    "SendAction",
    "plan",
    "split_text",
]
