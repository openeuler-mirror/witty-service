"""W4 命令解析与文案的测试（特性设计文档 9.1 第 11 条）。"""

from __future__ import annotations

import pytest

from witty_service.channels import commands
from witty_service.channels.errors import (
    CHANNEL_ACCESS_DENIED,
    CHANNEL_AGENT_NOT_BOUND,
    CHANNEL_AGENT_NOT_RUNNABLE,
    CHANNEL_CREDENTIAL_INVALID,
    CHANNEL_QUEUE_FULL,
    CHANNEL_UNSUPPORTED_CONTENT,
)

# ==============================================================================
# 解析
# ==============================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/help", "help"),
        ("/new", "new"),
        ("/stop", "stop"),
        ("/status", "status"),
        ("/version", "version"),
        ("/HELP", "help"),
        ("  /New  ", "new"),
        ("/new 帮我重新开始", "new"),
        ("/stop\n后面还有内容", "stop"),
    ],
)
def test_parses_known_commands(text: str, expected: str) -> None:
    parsed = commands.parse_command(text)

    assert parsed is not None
    assert parsed.name == expected
    assert parsed.raw == text.strip()


@pytest.mark.parametrize(
    "text",
    [
        "/unknown",
        "/helpme",
        "/",
        "/ ",
        "/new2",
        "hello",
        "帮我看看 /help 这条命令",
        "",
        "   ",
        None,
    ],
)
def test_unknown_or_plain_text_is_not_a_command(text: str | None) -> None:
    """以 / 开头但不在表内 -> 按普通消息处理（特性设计文档第 5 节）。"""
    assert commands.parse_command(text) is None
    assert commands.is_command(text) is False


def test_is_command_matches_parse() -> None:
    assert commands.is_command("/version") is True
    assert len(commands.KNOWN_COMMANDS) == 5


# ==============================================================================
# 文案渲染
# ==============================================================================


def test_render_help_lists_all_commands() -> None:
    text = commands.render_help(agent_bound=True)

    for name in commands.KNOWN_COMMANDS:
        assert f"/{name}" in text
    assert commands.HELP_NO_AGENT_SUFFIX not in text


def test_render_help_states_missing_agent() -> None:
    text = commands.render_help(agent_bound=False)

    assert commands.AGENT_NOT_BOUND_TEXT in text


def test_render_version_includes_channel_and_service_version() -> None:
    text = commands.render_version(channel="wecom_bot", adapter_version="1.0.0")

    assert "wecom_bot" in text
    assert "1.0.0" in text
    assert commands.service_version() in text


def test_service_version_falls_back_to_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib.metadata import PackageNotFoundError

    def _raise(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr(commands, "version", _raise)

    assert commands.service_version() == commands.UNKNOWN_VERSION


def test_render_status_unbound_agent() -> None:
    text = commands.render_status(
        commands.StatusView(agent_state="unbound", queue_depth=0)
    )

    assert commands.UNBOUND_STATUS_LABEL in text
    assert "尚未建立" in text
    assert "排队中的消息：0 条" in text


def test_render_status_deleted_agent() -> None:
    text = commands.render_status(
        commands.StatusView(
            agent_state="deleted",
            queue_depth=2,
            agent_name="demo",
            session_id="abcdef1234567890",
        )
    )

    assert commands.DELETED_STATUS_LABEL in text
    assert "排队中的消息：2 条" in text
    # 无标题 -> 显示短标识
    assert "abcdef12…" in text


def test_render_status_prefers_session_title() -> None:
    text = commands.render_status(
        commands.StatusView(
            agent_state="running",
            queue_depth=0,
            agent_name="demo",
            session_id="abcdef1234567890",
            session_title="部署排查",
        )
    )

    assert "部署排查" in text
    assert "demo（running）" in text


@pytest.mark.parametrize(
    ("kind", "label"),
    [("image", "图片"), ("file", "文件"), ("voice", "语音"), ("unknown", "暂不支持")],
)
def test_render_unsupported_content(kind: str, label: str) -> None:
    assert label in commands.render_unsupported_content(kind)


def test_render_queue_full_mentions_depth() -> None:
    assert "3 条" in commands.render_queue_full(depth=3)


def test_render_new_ack_reports_drained_messages() -> None:
    assert commands.render_new_ack(drained=0).endswith(commands.NEW_ACK_TEXT)
    assert "2 条" in commands.render_new_ack(drained=2)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (CHANNEL_AGENT_NOT_BOUND, commands.AGENT_NOT_BOUND_TEXT),
        (CHANNEL_CREDENTIAL_INVALID, commands.CREDENTIAL_INVALID_TEXT),
        (CHANNEL_ACCESS_DENIED, commands.ACCESS_DENIED_TEXT),
        ("SOMETHING_ELSE", commands.TURN_FAILED_TEXT),
    ],
)
def test_render_error_maps_known_codes(code: str, expected: str) -> None:
    assert commands.render_error(code=code) == expected


def test_render_error_uses_details_when_available() -> None:
    runnable = commands.render_error(
        code=CHANNEL_AGENT_NOT_RUNNABLE, agent_name="demo", status="error"
    )
    queue_full = commands.render_error(code=CHANNEL_QUEUE_FULL, depth=2)
    unsupported = commands.render_error(code=CHANNEL_UNSUPPORTED_CONTENT, kind="image")

    assert "demo" in runnable
    assert "error" in runnable
    assert "2 条" in queue_full
    assert "图片" in unsupported
