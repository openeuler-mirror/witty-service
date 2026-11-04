"""后端自带文案的多语言协商。

模板名称、描述、默认提问等展示文案由后端提供，随 UI 语言切换。
前端通过 ``Accept-Language``（或实例化请求里的显式 ``locale``）声明语言，
这里只负责把它规整成受支持的语言；模板层再按语言取对应文案，
未命中时一律回退中文。
"""

from __future__ import annotations

from fastapi import Header, Response

DEFAULT_LOCALE = "zh-CN"

#: 主语言子标签 -> 本项目支持的具体语言。只认完全相等的主标签（不做前缀匹配，
#: 避免 ``enigma`` 这类非法标签被误判成英文）。
_PRIMARY_LOCALES = {"zh": "zh-CN", "en": "en-US"}


def _parse_quality(params: str) -> float | None:
    """解析 ``;q=`` 参数。

    缺省为 1.0；非法值或 NaN 返回 ``None``（该条候选不可用），
    合法值 clamp 到 ``[0, 1]``（q=0 表示"明确不要"，由调用方丢弃）。
    """
    for param in params.split(";"):
        key, _, value = param.strip().partition("=")
        if key.strip().lower() != "q":
            continue
        try:
            quality = float(value)
        except (TypeError, ValueError):
            return None
        if quality != quality:  # NaN
            return None
        return min(1.0, max(0.0, quality))
    return 1.0


def normalize_locale(raw: str | None) -> str:
    """把 ``Accept-Language`` 或语言标签规整为受支持的语言。

    规则（按 RFC 4647 的朴素实现）：
    1. 逗号分段，逐段解析 q 值；非法 q、q<=0、空标签与通配符 ``*`` 直接丢弃；
    2. 主语言子标签与 ``zh`` / ``en`` 精确匹配（忽略大小写），
       命中即映射到 ``zh-CN`` / ``en-US``；
    3. 按 q 降序取第一条命中；q 相同时保持声明顺序；
    4. 没有任何命中时回退 ``zh-CN``。
    """
    if not raw or not isinstance(raw, str):
        return DEFAULT_LOCALE

    candidates: list[tuple[float, int, str]] = []
    for index, segment in enumerate(raw.split(",")):
        tag, _, params = segment.partition(";")
        tag = tag.strip()
        if not tag or tag == "*":
            continue
        quality = _parse_quality(params)
        if quality is None or quality <= 0:
            continue
        candidates.append((-quality, index, tag))

    for _, _, tag in sorted(candidates):
        primary = tag.split("-")[0].lower()
        locale = _PRIMARY_LOCALES.get(primary)
        if locale is not None:
            return locale
    return DEFAULT_LOCALE


def get_locale(
    accept_language: str | None = Header(default=None, alias="Accept-Language"),
) -> str:
    """FastAPI 依赖：从请求头协商界面语言。"""
    return normalize_locale(accept_language)


def set_locale_headers(response: Response, locale: str) -> None:
    """声明响应随 Accept-Language 变化，并标注实际返回的语言。"""
    # append：避免覆盖 CORS 等中间件已写入的 Vary
    response.headers.append("Vary", "Accept-Language")
    response.headers["Content-Language"] = locale


__all__ = [
    "DEFAULT_LOCALE",
    "get_locale",
    "normalize_locale",
    "set_locale_headers",
]
