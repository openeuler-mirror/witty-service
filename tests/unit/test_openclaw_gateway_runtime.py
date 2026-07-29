from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from witty_agent_server.infra.clients.base import ClientBase
from witty_agent_server.runtimes.openclaw_gateway_runtime import OpenClawGatewayRuntime


class StubGatewayClient(ClientBase):
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = events

    def list_agents(self) -> dict[str, Any]:
        return {}

    def list_sessions(self, *, agent_id: str) -> dict[str, Any]:
        return {"sessions": []}

    def get_agent(self, *, agent_id: str) -> dict[str, Any] | None:
        return None

    def get_skills_status(self, *, agent_id: str | None = None) -> dict[str, Any]:
        return {}

    def create_session(self, *, session_key: str) -> None:
        return None

    def delete_session(self, *, session_key: str) -> None:
        return None

    def abort_session(self, *, session_key: str) -> None:
        return None

    def stream_turn(
        self, *, session_key: str, message: str
    ) -> Iterator[dict[str, Any]]:
        del session_key, message
        yield from self._events


@pytest.mark.parametrize(
    ("payload", "expected_session_id"),
    [
        ({"sessionId": "runtime-session-top"}, "runtime-session-top"),
        ({"data": {"sessionId": "runtime-session-data"}}, "runtime-session-data"),
        (
            {"session": {"sessionId": "runtime-session-nested"}},
            "runtime-session-nested",
        ),
    ],
)
def test_run_turn_maps_sessions_changed_to_runtime_identity_event(
    payload: dict[str, Any],
    expected_session_id: str,
) -> None:
    runtime = OpenClawGatewayRuntime(
        client=StubGatewayClient(
            [
                {
                    "type": "sessions.changed",
                    "payload": payload,
                }
            ]
        )
    )

    events = list(runtime.run_turn(session_key="session-key", message="hello"))

    assert events == [
        {
            "type": "session.runtime.changed",
            "payload": {"runtime_session_id": expected_session_id},
        }
    ]


def test_run_turn_skips_sessions_changed_without_runtime_session_id() -> None:
    runtime = OpenClawGatewayRuntime(
        client=StubGatewayClient(
            [
                {
                    "type": "sessions.changed",
                    "payload": {},
                }
            ]
        )
    )

    events = list(runtime.run_turn(session_key="session-key", message="hello"))

    assert events == []


THINKING_FULL = (
    "The file has been written successfully. Let me count the characters "
    "to confirm it's roughly 1000 words. Let me do a quick check with wc."
)


def _thinking_delta_raw(delta: str, text: str) -> dict[str, Any]:
    return {
        "type": "agent",
        "payload": {"stream": "thinking", "data": {"delta": delta, "text": text}},
    }


def _assistant_message_raw() -> dict[str, Any]:
    return {
        "type": "session.message",
        "payload": {
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "content": [
                    {"type": "thinking", "thinking": THINKING_FULL},
                    {
                        "type": "toolCall",
                        "name": "exec",
                        "id": "call_00_faQXvcvaSjeSvomPZONp7427",
                        "arguments": {"command": "wc -m /tmp/a.md"},
                    },
                ],
            }
        },
    }


def test_run_turn_completes_thinking_tail_before_tool_call_started() -> None:
    """完整 thinking 先于迟到的块尾 delta 到达时，补发尾巴并吞掉迟到 delta。"""
    runtime = OpenClawGatewayRuntime(
        client=StubGatewayClient(
            [
                _thinking_delta_raw(" w", THINKING_FULL[: -len("c.")]),
                _assistant_message_raw(),
                # 迟到的块尾 delta，在 tool.call.started 之后到达
                _thinking_delta_raw("c.", THINKING_FULL),
            ]
        )
    )

    events = list(runtime.run_turn(session_key="session-key", message="hello"))

    assert [e["type"] for e in events] == [
        "thinking.delta",
        "thinking.delta",
        "thinking",
        "tool.call.started",
    ]
    # 补发的尾巴 delta 排在 thinking / tool.call.started 之前
    assert events[1]["payload"] == {"delta": "c.", "text": THINKING_FULL}
    # 所有 delta 拼接应等于完整 thinking，无缺失、无重复
    streamed = "".join(
        e["payload"]["delta"] for e in events if e["type"] == "thinking.delta"
    )
    assert streamed == " wc."


def test_run_turn_suppresses_late_thinking_tail_split_into_chunks() -> None:
    """迟到尾巴被拆成多个 delta 时，逐个抵扣丢弃。"""
    runtime = OpenClawGatewayRuntime(
        client=StubGatewayClient(
            [
                _thinking_delta_raw(" w", THINKING_FULL[: -len("c.")]),
                _assistant_message_raw(),
                _thinking_delta_raw("c", THINKING_FULL[:-1]),
                _thinking_delta_raw(".", THINKING_FULL),
            ]
        )
    )

    events = list(runtime.run_turn(session_key="session-key", message="hello"))

    assert [e["type"] for e in events] == [
        "thinking.delta",
        "thinking.delta",
        "thinking",
        "tool.call.started",
    ]
    # 补发的尾巴 delta 排在 thinking / tool.call.started 之前
    assert events[1]["payload"] == {"delta": "c.", "text": THINKING_FULL}
    # 所有 delta 拼接应等于完整 thinking，无缺失、无重复
    streamed = "".join(
        e["payload"]["delta"] for e in events if e["type"] == "thinking.delta"
    )
    assert streamed == " wc."


def test_run_turn_passes_through_thinking_delta_beyond_suppressed_tail() -> None:
    """迟到 delta 超出已补发尾巴时，只下发剩余部分，不丢文本。"""
    runtime = OpenClawGatewayRuntime(
        client=StubGatewayClient(
            [
                _thinking_delta_raw(" w", THINKING_FULL[: -len("c.")]),
                _assistant_message_raw(),
                _thinking_delta_raw("c. Next", THINKING_FULL + " Next"),
            ]
        )
    )

    events = list(runtime.run_turn(session_key="session-key", message="hello"))

    assert [e["type"] for e in events] == [
        "thinking.delta",
        "thinking.delta",
        "thinking",
        "tool.call.started",
        "thinking.delta",
    ]
    assert events[-1]["payload"]["delta"] == " Next"


def test_run_turn_without_missing_thinking_tail_passes_through() -> None:
    """delta 流已完整时不补发，事件按原样透传。"""
    runtime = OpenClawGatewayRuntime(
        client=StubGatewayClient(
            [
                _thinking_delta_raw(THINKING_FULL, THINKING_FULL),
                _assistant_message_raw(),
            ]
        )
    )

    events = list(runtime.run_turn(session_key="session-key", message="hello"))

    assert [e["type"] for e in events] == [
        "thinking.delta",
        "thinking",
        "tool.call.started",
    ]
    assert events[0]["payload"]["delta"] == THINKING_FULL


# ---------------------------------------------------------------------------
# 跨 thinking block 的 suppress 抵扣
# ---------------------------------------------------------------------------

THINKING_BLOCK2 = "Let me review the results."


def _two_block_assistant_raw() -> dict[str, Any]:
    return {
        "type": "session.message",
        "payload": {
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "content": [
                    {"type": "thinking", "thinking": THINKING_FULL},
                    {
                        "type": "toolCall",
                        "name": "exec",
                        "id": "call_00_faQXvcvaSjeSvomPZONp7427",
                        "arguments": {"command": "wc -m /tmp/a.md"},
                    },
                    {"type": "thinking", "thinking": THINKING_BLOCK2},
                    {
                        "type": "toolCall",
                        "name": "read",
                        "id": "call_01_example",
                        "arguments": {"file": "/tmp/a.md"},
                    },
                ],
            }
        },
    }


def test_run_turn_suppress_not_leak_across_thinking_blocks() -> None:
    """suppress 不会跨 thinking block 泄漏——第二个 block 到达时 suppress 被清空。"""
    runtime = OpenClawGatewayRuntime(
        client=StubGatewayClient(
            [
                _thinking_delta_raw(
                    THINKING_FULL[:10], THINKING_FULL[:10]
                ),
                _two_block_assistant_raw(),
                # 第一个 block 的迟到 delta（suppress 已被 block2 清空，正常透传）
                _thinking_delta_raw(THINKING_FULL[10:], THINKING_FULL),
                # 第二个 block 的 delta（正常透传）
                _thinking_delta_raw(
                    THINKING_BLOCK2, THINKING_BLOCK2
                ),
            ]
        )
    )

    events = list(runtime.run_turn(session_key="session-key", message="hello"))

    types = [e["type"] for e in events]
    # 同一条 session.message 中包含两个 thinking block 时，
    # _on_thinking_event 处理 block2 时会在开头清空 block1 的 suppress，
    # 因此迟到 delta 不会被 suppress 吞掉而是正常透传。
    assert types == [
        "thinking.delta",      # block1 initial delta
        "thinking.delta",      # 补发的 block1 尾巴
        "thinking",            # block1 complete
        "thinking",            # block2 complete
        "tool.call.started",   # exec
        "tool.call.started",   # read
        "thinking.delta",      # block1 迟到 delta（未被抑制，正常透传）
        "thinking.delta",      # block2 delta（正常透传）
    ]


# ---------------------------------------------------------------------------
# empty / None thinking 字段
# ---------------------------------------------------------------------------


def _empty_thinking_assistant_raw() -> dict[str, Any]:
    return {
        "type": "session.message",
        "payload": {
            "message": {
                "role": "assistant",
                "stopReason": "toolUse",
                "content": [
                    {"type": "thinking", "thinking": ""},
                    {
                        "type": "toolCall",
                        "name": "exec",
                        "id": "call_00_faQXvcvaSjeSvomPZONp7427",
                        "arguments": {"command": "wc -m /tmp/a.md"},
                    },
                ],
            }
        },
    }


def test_run_turn_empty_thinking_filtered_by_extractor() -> None:
    """_extract_thinking_events 过滤空 thinking，不产出 thinking 事件。"""
    runtime = OpenClawGatewayRuntime(
        client=StubGatewayClient(
            [
                _thinking_delta_raw(" w", THINKING_FULL[: -len("c.")]),
                _empty_thinking_assistant_raw(),
            ]
        )
    )

    events = list(runtime.run_turn(session_key="session-key", message="hello"))

    # _extract_thinking_events 跳过 thinking="" 的内容块，
    # 因此空 thinking 不会被产出，只有 tool.call.started
    assert [e["type"] for e in events] == [
        "thinking.delta",
        "tool.call.started",
    ]
