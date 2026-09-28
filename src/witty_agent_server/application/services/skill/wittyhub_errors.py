"""wittyhub CLI 失败输出的清洗与分类。

wittyhub CLI 失败时将错误文本打印到 stdout（带 ASCII banner 与 ANSI 颜色），
本模块负责：
1. 清洗原始输出（剥离 ANSI 转义序列、过滤 banner 装饰行、截断）；
2. 按已知输出子串将失败归类为结构化错误码，并生成可读文案。

分类子串来源于 wittyhub@0.0.5 实际输出（dist/cli.mjs）；CLI 版本升级后
若文案变化，需同步更新 ``_CLASSIFICATION_RULES``。
"""

from __future__ import annotations

import re

from witty_agent_server.application.services.skill.errors import (
    WITTYHUB_BAD_SOURCE,
    WITTYHUB_HUB_HTTP_ERROR,
    WITTYHUB_HUB_UNREACHABLE,
    WITTYHUB_REPO_NOT_INDEXED,
    WITTYHUB_SKILL_NOT_FOUND,
)

# 清洗后的原始输出最大保留长度
RAW_OUTPUT_MAX_LENGTH = 800

# ANSI 转义序列（CSI 与 OSC）
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
)

# hub HTTP 状态码（规则匹配与消息提取共用）
_HTTP_STATUS_RE = re.compile(r"HTTP\s*(\d{3})")

# box-drawing / 块字符 / 常见装饰字符，用于识别 banner 装饰行
_DECORATIVE_CHARS = set(
    "─━│┃┄┅┆┇┈┉┊┋┌┍┎┏┐┑┒┓└┕┖┗┘┙┚┛├┝┞┟┠┡┢┣┤┥┦┧┨┩┪┫┬┭┮┯┰┱╱╲╳"
    "┴┵┶┷┸┹┺┻┼╁╂╃╄╅╆╇╈╉╊╋╌╍╎╏═║╒╓╔╕╖╗╘╙╚╛╜╝╞╟╠╡╢╣╤╥╦╧╨╩╪╫╬"
    "╭╮╯╰╴╵╶╷╸╹╺╻╼╽╾╿█▉▊▋▌▍▎▏▀▁▂▃▄▅▆▇▓▒░ =*~_-"
)

# 分类规则：按顺序匹配（先特异后泛化），命中即返回对应 code。
# 子串来自 wittyhub@0.0.5 dist/cli.mjs 的实际失败输出。
_CLASSIFICATION_RULES: list[tuple[str, re.Pattern[str]]] = [
    (WITTYHUB_BAD_SOURCE, re.compile(r"Local path does not exist")),
    # "仓库 xxx 下未找到已收录的技能" 须先于 SKILL_NOT_FOUND（子串重叠）
    (
        WITTYHUB_REPO_NOT_INDEXED,
        re.compile(r"仓库.*未找到已收录的技能"),
    ),
    (
        WITTYHUB_SKILL_NOT_FOUND,
        re.compile(
            r"未找到匹配 --skill|未找到名称匹配|No matching skills found|未找到已收录的技能"
        ),
    ),
    (
        WITTYHUB_HUB_HTTP_ERROR,
        _HTTP_STATUS_RE,
    ),
    (
        WITTYHUB_HUB_UNREACHABLE,
        re.compile(
            r"无法连接搜索服务|Failed to download skill|[Nn]etwork error|"
            r"ECONNREFUSED|ETIMEDOUT|ENOTFOUND|getaddrinfo"
        ),
    ),
]


def _is_banner_line(line: str) -> bool:
    """判断是否为 banner 装饰行（空白或装饰字符占比过半）。"""
    stripped = line.strip()
    if not stripped:
        return True
    decorative = sum(1 for ch in stripped if ch in _DECORATIVE_CHARS)
    return decorative / len(stripped) >= 0.5


def sanitize_wittyhub_output(output: str) -> str:
    """清洗 wittyhub 原始输出：剥 ANSI、去 banner 行、截断。"""
    if not output:
        return ""
    text = _ANSI_ESCAPE_RE.sub("", output)
    lines = [line.strip() for line in text.splitlines() if not _is_banner_line(line)]
    cleaned = "\n".join(lines).strip()
    if len(cleaned) > RAW_OUTPUT_MAX_LENGTH:
        cleaned = cleaned[:RAW_OUTPUT_MAX_LENGTH] + "...(truncated)"
    return cleaned


def _last_meaningful_line(text: str) -> str:
    """返回文本中最后一个非空有效行。"""
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def extract_core_message(output: str) -> str:
    """清洗输出并抽取最后一个非空有效行，作为失败的核心文本。"""
    return _last_meaningful_line(sanitize_wittyhub_output(output))


def _extract_http_status(cleaned: str) -> str | None:
    match = _HTTP_STATUS_RE.search(cleaned)
    return match.group(1) if match else None


def classify_wittyhub_failure(
    *,
    stdout: str,
    stderr: str,
    returncode: int,
    skill_name: str,
    skill_source: str | None = None,
) -> tuple[str | None, str, str]:
    """将 wittyhub CLI 失败输出分类为 (code, friendly_message, raw_output)。

    * code: 命中已知模式时为 ``WITTYHUB_*`` 错误码，未命中时为 None
      （由调用方沿用其兜底错误类）；
    * friendly_message: 可读英文文案，写入 details.reason；
    * raw_output: 清洗后的原始输出（供排障），无内容时为空串。
    """
    combined = "\n".join(part for part in (stdout, stderr) if part)
    cleaned = sanitize_wittyhub_output(combined)

    code: str | None = None
    for candidate_code, pattern in _CLASSIFICATION_RULES:
        if pattern.search(cleaned):
            code = candidate_code
            break

    source = skill_source or "wittyhub"
    if code == WITTYHUB_SKILL_NOT_FOUND:
        message = (
            f'skill "{skill_name}" was not found in source "{source}". '
            "Check the skill name, or verify the source repository is "
            "registered in the skill hub."
        )
    elif code == WITTYHUB_REPO_NOT_INDEXED:
        message = (
            f'source repository "{source}" is not indexed in the skill hub '
            "yet; no skills could be resolved from it."
        )
    elif code == WITTYHUB_HUB_UNREACHABLE:
        message = "unable to reach the skill hub service; check network connectivity."
    elif code == WITTYHUB_HUB_HTTP_ERROR:
        status = _extract_http_status(cleaned)
        suffix = f" (HTTP {status})" if status else ""
        message = (
            f"skill hub service returned an error{suffix}; "
            "retry later or contact the hub administrator."
        )
    elif code == WITTYHUB_BAD_SOURCE:
        message = f'skill source "{source}" is invalid: local path does not exist.'
    else:
        code = None
        message = (
            _last_meaningful_line(cleaned) or f"wittyhub exited with code {returncode}"
        )

    return code, message, cleaned
