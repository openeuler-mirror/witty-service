"""命令解析与用户可见文案。

本模块是渠道层**唯一**的用户可见文案出处（框架设计 §8.1 的"对用户的表现"、
特性设计文档第 5 节的命令集）。解析与渲染都是纯函数，便于表驱动测试。

命令解析规则（特性设计文档第 5 节）：

- 只有表内的五条命令才是命令；
- **以 `/` 开头但不在表内的内容按普通消息处理**并交给 agent——用户完全可能
  与 agent 讨论一段以 `/` 开头的文本，此时报错才是错的。
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version

from witty_service.channels import errors as err
from witty_service.channels.contracts import UNSUPPORTED_KINDS

# ==============================================================================
# 命令解析
# ==============================================================================

COMMAND_HELP = "help"
COMMAND_NEW = "new"
COMMAND_STOP = "stop"
COMMAND_STATUS = "status"
COMMAND_VERSION = "version"

#: 命令集（特性设计文档第 5 节）。新增命令必须同时更新 `render_help`。
KNOWN_COMMANDS: tuple[str, ...] = (
    COMMAND_HELP,
    COMMAND_NEW,
    COMMAND_STOP,
    COMMAND_STATUS,
    COMMAND_VERSION,
)

COMMAND_PREFIX = "/"

UNKNOWN_VERSION = "unknown"


@dataclass(frozen=True, slots=True)
class ParsedCommand:
    """命中的命令。`raw` 保留用户原始输入，便于排障与命令回执。"""

    name: str
    raw: str


def parse_command(text: str | None) -> ParsedCommand | None:
    """解析命令；返回 None 表示"这是一条普通消息"。

    大小写不敏感，允许尾随参数（`/new 一下` 与 `/new` 等价），因为私聊里
    用户的输入习惯不可控，而命令本身没有参数语法。
    """
    if text is None:
        return None
    stripped = text.strip()
    if not stripped.startswith(COMMAND_PREFIX):
        return None
    parts = stripped[len(COMMAND_PREFIX) :].split(maxsplit=1)
    if not parts:
        return None
    token = parts[0].lower()
    if token not in KNOWN_COMMANDS:
        return None
    return ParsedCommand(name=token, raw=stripped)


def is_command(text: str | None) -> bool:
    return parse_command(text) is not None


# ==============================================================================
# 文案常量
# ==============================================================================

# --- 回合进行中的呈现文案（供 router / delivery 共用） ---
PLACEHOLDER_TEXT = "收到，正在处理…"
DEFERRED_NOTICE_TEXT = "还在处理，完成后我会把结果发给你。"
CONSOLE_NOTICE_TEXT = "（内容较长，完整结果请在控制台查看）"
EMPTY_RESULT_TEXT = "（本次没有产生可展示的文本结果）"
DELIVERY_FAILED_TEXT = "结果已生成，但发送失败，请在控制台查看。"

#: 连通性测试文案（`POST /channels/instances/{id}/test`）：只发这一条固定文案，
#: 不触发回合、不写会话历史（框架设计 §9）
CONNECTIVITY_TEST_TEXT = "这是一条连通性测试消息，收到即表示渠道连接正常。"

# --- 命令回执 ---
STOP_ACK_TEXT = "已停止当前回合。"
STOP_IDLE_ACK_TEXT = "当前没有正在处理的回合。"
STOP_CANCELLED_TEXT = "这条排队中的消息已随停止命令取消。"
NEW_ACK_TEXT = "已开启新会话，后续消息将进入新会话。"
NEW_QUEUE_DRAINED_SUFFIX = "（已取消 {count} 条排队中的消息）"

# --- 明确降级 / 错误文案 ---
AGENT_NOT_BOUND_TEXT = "该机器人当前没有可用的 agent，请联系部署方处理。"
CREDENTIAL_INVALID_TEXT = "机器人凭据已失效，需重新接入。"
TURN_FAILED_TEXT = "处理这条消息时出错了，请稍后重试。"
INTERACTION_NOTICE_TEXT = "该请求需要在控制台处理。"
ACCESS_DENIED_TEXT = "你当前未获授权使用该机器人，请联系部署方处理。"
UNSUPPORTED_CONTENT_TEMPLATE = "当前只支持文本消息，收到的是一条{label}，请改用文字发送。"
AGENT_NOT_RUNNABLE_TEMPLATE = "agent「{name}」当前状态为 {status}，暂时无法处理消息。"
QUEUE_FULL_TEMPLATE = "前面还有 {depth} 条消息在排队，这条未被受理，请稍后再发。"
STATUS_TEMPLATE = (
    "当前状态：\n"
    "- agent：{agent}\n"
    "- 当前会话：{session}\n"
    "- 排队中的消息：{queue_depth} 条"
)
HELP_TEMPLATE = (
    "可用命令：\n"
    "- /help 查看本说明\n"
    "- /new 开启新会话（旧会话保留在控制台）\n"
    "- /stop 停止当前回合并清空排队消息\n"
    "- /status 查看 agent 状态、当前会话与排队条数\n"
    "- /version 查看服务版本与渠道适配器版本"
)
HELP_NO_AGENT_SUFFIX = "\n\n提示：该机器人当前没有可用的 agent，请联系部署方处理。"
VERSION_TEMPLATE = "witty-service {service_version}；渠道适配器 {channel} {adapter_version}"

UNSUPPORTED_KIND_LABELS: dict[str, str] = {
    "image": "图片",
    "file": "文件",
    "voice": "语音",
    "unknown": "暂不支持的内容",
}

UNBOUND_STATUS_LABEL = "未绑定"
DELETED_STATUS_LABEL = "已删除（绑定后已不可用）"


# ==============================================================================
# 渲染
# ==============================================================================


def service_version() -> str:
    """服务版本：取不到时回退常量 `unknown`（特性设计文档第 5 节的 /version）。"""
    try:
        return version("witty-service")
    except PackageNotFoundError:
        return UNKNOWN_VERSION
    except Exception:  # pragma: no cover - importlib.metadata 的兜底
        return UNKNOWN_VERSION


def render_help(*, agent_bound: bool) -> str:
    text = HELP_TEMPLATE
    if not agent_bound:
        text += HELP_NO_AGENT_SUFFIX
    return text


def render_version(*, channel: str, adapter_version: str) -> str:
    return VERSION_TEMPLATE.format(
        service_version=service_version(),
        channel=channel,
        adapter_version=adapter_version,
    )


@dataclass(frozen=True, slots=True)
class StatusView:
    """`/status` 的输入快照。`agent_state` 取值：agent 状态值 / `unbound` / `deleted`。"""

    agent_state: str
    queue_depth: int
    agent_name: str | None = None
    session_id: str | None = None
    session_title: str | None = None


def render_status(view: StatusView) -> str:
    if view.agent_state == "unbound":
        agent_label = UNBOUND_STATUS_LABEL
    elif view.agent_state == "deleted":
        agent_label = f"{DELETED_STATUS_LABEL}"
    else:
        agent_label = (
            f"{view.agent_name}（{view.agent_state}）"
            if view.agent_name
            else view.agent_state
        )
    return STATUS_TEMPLATE.format(
        agent=agent_label,
        session=_render_session(view),
        queue_depth=view.queue_depth,
    )


def _render_session(view: StatusView) -> str:
    """有标题时显示标题，否则显示短标识（特性设计文档第 5 节）。"""
    if view.session_id is None:
        return "尚未建立"
    if view.session_title:
        return view.session_title
    return f"{view.session_id[:8]}…"


def render_unsupported_content(kind: str | None) -> str:
    label = UNSUPPORTED_KIND_LABELS.get(
        kind or "unknown", UNSUPPORTED_KIND_LABELS["unknown"]
    )
    if kind is not None and kind not in UNSUPPORTED_KINDS:
        label = UNSUPPORTED_KIND_LABELS["unknown"]
    return UNSUPPORTED_CONTENT_TEMPLATE.format(label=label)


def render_agent_not_runnable(*, agent_name: str | None, status: str) -> str:
    return AGENT_NOT_RUNNABLE_TEMPLATE.format(
        name=agent_name or "该", status=status
    )


def render_queue_full(*, depth: int) -> str:
    return QUEUE_FULL_TEMPLATE.format(depth=depth)


def render_new_ack(*, drained: int) -> str:
    text = NEW_ACK_TEXT
    if drained > 0:
        text += NEW_QUEUE_DRAINED_SUFFIX.format(count=drained)
    return text


def render_error(
    *,
    code: str,
    agent_name: str | None = None,
    status: str | None = None,
    depth: int | None = None,
    kind: str | None = None,
) -> str:
    """把域错误码翻译成用户可见文案（`SessionRouter` 只负责调用本函数）。"""
    if code == err.CHANNEL_AGENT_NOT_BOUND:
        return AGENT_NOT_BOUND_TEXT
    if code == err.CHANNEL_AGENT_NOT_RUNNABLE:
        return render_agent_not_runnable(
            agent_name=agent_name, status=status or "unknown"
        )
    if code == err.CHANNEL_CREDENTIAL_INVALID:
        return CREDENTIAL_INVALID_TEXT
    if code == err.CHANNEL_QUEUE_FULL:
        return render_queue_full(depth=depth or 0)
    if code == err.CHANNEL_UNSUPPORTED_CONTENT:
        return render_unsupported_content(kind)
    if code == err.CHANNEL_ACCESS_DENIED:
        return ACCESS_DENIED_TEXT
    if code == err.CHANNEL_DELIVERY_UNCERTAIN:
        return DELIVERY_FAILED_TEXT
    return TURN_FAILED_TEXT


__all__ = [
    "ACCESS_DENIED_TEXT",
    "AGENT_NOT_BOUND_TEXT",
    "AGENT_NOT_RUNNABLE_TEMPLATE",
    "COMMAND_HELP",
    "COMMAND_NEW",
    "COMMAND_PREFIX",
    "COMMAND_STATUS",
    "COMMAND_STOP",
    "COMMAND_VERSION",
    "CONSOLE_NOTICE_TEXT",
    "CREDENTIAL_INVALID_TEXT",
    "DEFERRED_NOTICE_TEXT",
    "DELIVERY_FAILED_TEXT",
    "EMPTY_RESULT_TEXT",
    "INTERACTION_NOTICE_TEXT",
    "KNOWN_COMMANDS",
    "NEW_ACK_TEXT",
    "PLACEHOLDER_TEXT",
    "STOP_ACK_TEXT",
    "STOP_CANCELLED_TEXT",
    "STOP_IDLE_ACK_TEXT",
    "TURN_FAILED_TEXT",
    "UNKNOWN_VERSION",
    "ParsedCommand",
    "StatusView",
    "is_command",
    "parse_command",
    "render_error",
    "render_help",
    "render_new_ack",
    "render_queue_full",
    "render_status",
    "render_unsupported_content",
    "render_version",
    "service_version",
]
