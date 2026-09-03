"""``RuntimeBase._on_artifact_event`` 的统一 artifact 产出钩子单测。

重构后各 runtime 不再各自检测 ``write`` 工具 / 缓存参数，统一由基类
``_on_artifact_event`` 据标准 ``tool.call.*`` 事件产出 ``artifact.*`` 事件。
该单测直接驱动基类默认实现，不依赖任何具体 runtime / SDK。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from witty_agent_server.runtimes.runtime_base import (
    RuntimeBase,
    RuntimeTurnEvent,
    RuntimeType,
    TurnEventType,
)

_WRITE_ARGS = {"file_path": "output/demo.html", "content": "<h1>hi</h1>"}


class _FakeRuntime(RuntimeBase):
    """最小实现，仅用于驱动基类的 ``_on_artifact_event``。"""

    runtime_type: RuntimeType = "opencode"

    def _map_events(self, raw: dict[str, Any]) -> Iterator[RuntimeTurnEvent]:
        del raw
        return iter(())


def _run_artifact_hook(
    runtime: _FakeRuntime, *events: RuntimeTurnEvent
) -> list[dict[str, Any]]:
    runtime._turn.artifact_inputs_by_call_id = {}
    out: list[dict[str, Any]] = []
    for event in events:
        out.extend(runtime._on_artifact_event(event))
    return out


def _tool_started(
    *,
    tool_name: str = "write",
    call_id: str = "call-1",
    arguments: Any = _WRITE_ARGS,
) -> RuntimeTurnEvent:
    return {
        "type": TurnEventType.TOOL_CALL_STARTED,
        "payload": {
            "stage": "started",
            "tool_name": tool_name,
            "tool_call_id": call_id,
            "arguments": arguments,
        },
    }


def _tool_response(
    *,
    name: str = "write",
    call_id: str = "call-1",
    is_error: bool = False,
    content: str = "written",
) -> RuntimeTurnEvent:
    return {
        "type": TurnEventType.TOOL_CALL_RESPONSE,
        "payload": {
            "stage": "response",
            "name": name,
            "tool_call_id": call_id,
            "content": content,
            "is_error": is_error,
        },
    }


def test_started_for_write_emits_artifact_started() -> None:
    runtime = _FakeRuntime()
    events = _run_artifact_hook(runtime, _tool_started())

    assert [e["type"] for e in events] == ["artifact.started"]
    payload = events[0]["payload"]
    assert payload["id"] == "output/demo.html"
    assert payload["status"] == "creating"
    assert payload["mime"] == "text/html"
    assert "content" not in payload


def test_started_then_response_emits_completed_with_content() -> None:
    runtime = _FakeRuntime()
    events = _run_artifact_hook(
        runtime, _tool_started(), _tool_response()
    )

    assert [e["type"] for e in events] == ["artifact.started", "artifact.completed"]
    completed = events[1]["payload"]
    assert completed["status"] == "ready"
    assert completed["content"] == "<h1>hi</h1>"
    assert completed["size"] == len(b"<h1>hi</h1>")


def test_response_error_marks_artifact_error() -> None:
    runtime = _FakeRuntime()
    events = _run_artifact_hook(
        runtime, _tool_started(), _tool_response(is_error=True)
    )

    completed = events[1]["payload"]
    assert completed["status"] == "error"
    assert "content" not in completed


def test_non_write_tool_emits_no_artifact() -> None:
    runtime = _FakeRuntime()
    events = _run_artifact_hook(
        runtime, _tool_started(tool_name="read"), _tool_response(name="read")
    )
    assert events == []


def test_response_without_cached_args_emits_no_artifact() -> None:
    runtime = _FakeRuntime()
    # 只有 response、没有先前的 started（无缓存参数），应优雅跳过。
    events = _run_artifact_hook(runtime, _tool_response())
    assert events == []


def test_started_with_non_dict_arguments_skips() -> None:
    runtime = _FakeRuntime()
    events = _run_artifact_hook(runtime, _tool_started(arguments="not-a-dict"))
    assert events == []
    assert runtime._turn.artifact_inputs_by_call_id == {}


def test_started_with_json_string_arguments_caches_and_completes() -> None:
    # 合法 JSON 字符串 arguments 也应在 started 时归一化缓存，completed 复用同一份 dict。
    runtime = _FakeRuntime()
    arguments = '{"file_path": "output/demo.html", "content": "<h1>hi</h1>"}'
    events = _run_artifact_hook(
        runtime, _tool_started(arguments=arguments), _tool_response()
    )
    assert [e["type"] for e in events] == ["artifact.started", "artifact.completed"]
    assert events[1]["payload"]["content"] == "<h1>hi</h1>"
    assert runtime._turn.artifact_inputs_by_call_id["call-1"] == {
        "file_path": "output/demo.html",
        "content": "<h1>hi</h1>",
    }


def test_response_uses_name_key_to_detect_write() -> None:
    # response 事件用 ``name`` 而非 ``tool_name``，钩子应能统一识别。
    runtime = _FakeRuntime()
    events = _run_artifact_hook(
        runtime, _tool_started(), _tool_response(name="write")
    )
    assert [e["type"] for e in events] == ["artifact.started", "artifact.completed"]

    # 若 response 的 name 不是 write，则不产出 completed。
    runtime2 = _FakeRuntime()
    events2 = _run_artifact_hook(
        runtime2, _tool_started(), _tool_response(name="read")
    )
    assert [e["type"] for e in events2] == ["artifact.started"]
