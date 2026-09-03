"""dsh runtime 真实链路冒烟测试。

覆盖实施方案 P5 与验收清单核心路径：
``WITTY_RUNTIME_DEFAULT=dsh`` 启动 agent-server → agent start → RUNNING →
WS 发消息 → 收到 ``message.started`` / ``message.delta`` / ``message.completed``
→ 工具事件（若本轮触发）→ ``turn.completed``。

需要真实 LLM 凭证：未设置 ``DEEPSEEK_API_KEY`` 时整模块跳过
（本地 env / CI secret 注入）。api_key/base_url 由 dsh 子进程继承调用方
环境（SDK ``DeepSeekHarnessConfig`` 默认继承 env），无需显式下发。
"""

from __future__ import annotations

import contextlib
import os
import threading
from typing import Any

import pytest
from fastapi.testclient import TestClient

from witty_agent_server.app import create_app

# 未注入真实 key 时整模块跳过（冒烟测试默认不参与普通回归）。
pytestmark = pytest.mark.skipif(
    not os.environ.get("DEEPSEEK_API_KEY"),
    reason="DEEPSEEK_API_KEY not set; skipping dsh real-chain smoke test",
)

_AGENT_ID = "dsh-smoke"
# 真实 LLM 调用 + 可能的工具执行，宽松上限避免网络抖动误报。
_TURN_TIMEOUT_SECONDS = 120.0
_TURN_TERMINAL_TYPES = {"turn.completed"}


def _configure_env(monkeypatch: pytest.MonkeyPatch, workspace_root: Any) -> None:
    """隔离测试环境：dsh 为默认 runtime，workspace 指向临时目录。"""
    monkeypatch.setenv("WITTY_RUNTIME_DEFAULT", "dsh")
    monkeypatch.setenv("WITTY_RUNTIME_SUPPORTED", "openclaw,opencode,dsh")
    monkeypatch.setenv("WITTY_WORKSPACE_ROOT", str(workspace_root))
    # 重置全局 settings 单例，使上面 env 生效。
    monkeypatch.setattr("witty_service.config._settings", None)


def _drain_ws_events(
    ws: Any, *, stop_types: set[str], timeout: float
) -> list[dict[str, Any]]:
    """从 WS 收集事件直到命中终止事件；超时则抛 AssertionError。

    用独立线程 + join 保证阻塞式 ``receive_json`` 也能被 deadline 中断，
    避免真实链路异常（LLM 无响应等）时测试无限悬挂。
    """
    collected: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def _receive() -> None:
        try:
            while True:
                event = ws.receive_json()
                collected.append(event)
                if event.get("type") in stop_types:
                    return
        except BaseException as exc:
            errors.append(exc)

    receiver = threading.Thread(target=_receive, daemon=True)
    receiver.start()
    receiver.join(timeout)
    if receiver.is_alive():
        seen = [e.get("type") for e in collected]
        raise AssertionError(
            f"dsh smoke timed out after {timeout:.0f}s waiting for {sorted(stop_types)}; "
            f"events seen so far: {seen}"
        )
    if errors:
        raise AssertionError(f"dsh smoke ws receive failed: {errors[0]}")
    return collected


def _assert_turn_sequence(events: list[dict[str, Any]]) -> None:
    """校验统一事件序列，工具事件成对时校验 tool_call_id 配对。"""
    types = [e.get("type") for e in events]
    assert "message.started" in types, f"missing message.started; events={types}"
    assert "message.completed" in types, f"missing message.completed; events={types}"
    assert "turn.completed" in types, f"missing turn.completed; events={types}"
    assert "stream.error" not in types, f"unexpected stream.error; events={types}"

    completed = next(e for e in events if e.get("type") == "message.completed")
    text = completed.get("payload", {}).get("text")
    assert isinstance(text, str) and text.strip(), (
        f"message.completed must carry non-empty text; payload={completed.get('payload')}"
    )

    started_ids = [
        e.get("payload", {}).get("tool_call_id")
        for e in events
        if e.get("type") == "tool.call.started"
    ]
    response_ids = [
        e.get("payload", {}).get("tool_call_id")
        for e in events
        if e.get("type") == "tool.call.response"
    ]
    for call_id in started_ids:
        assert call_id in response_ids, (
            f"tool.call.started without matching tool.call.response: {call_id}"
        )


def test_dsh_smoke_turn(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """start → RUNNING → WS 发消息 → 收 started/delta/completed → turn.completed。"""
    _configure_env(monkeypatch, tmp_path / "witty-workspace")

    app = create_app(runtime_type="dsh")
    with TestClient(app) as client:
        # 1. agent start → RUNNING（dsh 子进程 spawn + initialize 握手）。
        resp = client.post("/agent/start", params={"id": _AGENT_ID}, json={})
        assert resp.status_code == 200, f"agent start failed: {resp.text}"
        assert resp.json()["status"] == "running"

        try:
            # 2. create session（runtime_type 由 bundle 默认注册解析为 dsh）。
            resp = client.post(f"/agents/{_AGENT_ID}/sessions", json={})
            assert resp.status_code == 200, f"session create failed: {resp.text}"
            session_id = resp.json()["id"]
            assert resp.json()["runtime_type"] == "dsh"

            # 3. WS 发消息：触发一次真实 LLM 轮次（提示词倾向触发 bash 工具）。
            ws_url = f"/agents/{_AGENT_ID}/sessions/{session_id}/ws"
            with client.websocket_connect(ws_url) as ws:
                ws.send_json(
                    {
                        "type": "message.create",
                        "payload": {
                            "message": (
                                "请用 bash 执行 `echo dsh-smoke-ok`，"
                                "把输出原样告诉我；如果没有工具就回复 OK。"
                            )
                        },
                    }
                )
                events = _drain_ws_events(
                    ws,
                    stop_types=_TURN_TERMINAL_TYPES,
                    timeout=_TURN_TIMEOUT_SECONDS,
                )

            # 4. 校验统一事件序列。
            _assert_turn_sequence(events)
            seen = [e.get("type") for e in events]
            assert "message.delta" in seen, (
                f"missing streaming message.delta; events={seen}"
            )
            tool_types = [t for t in seen if t.startswith("tool.")]
            print(
                "dsh smoke turn ok:",
                f"events={len(events)}",
                f"tool_events={tool_types}",
            )
        finally:
            # 无论断言结果如何都关闭 harness，避免子进程泄漏。
            with contextlib.suppress(Exception):
                client.post("/agent/stop", params={"id": _AGENT_ID})
