# Witty-Service Message SSE Streaming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留 `POST /messages` 非流式接口的前提下，新增 `POST /messages/stream` SSE 流式接口，并统一消息契约为“事件 envelope 透传上游 + `sandbox_type` 由 witty-service 顶层提供”。

**Architecture:** 继续以 `witty-agent-server` 的 WebSocket 流作为单一事实源。`AgentManager` 提供两种消费模式：聚合模式（非流式）和逐条转发模式（SSE）。API 层负责协议封装：非流式返回 `{sandbox_type, events}`，SSE 返回 `data: {sandbox_type, event}`。

**Tech Stack:** FastAPI, Pydantic, asyncio, websockets, pytest

---

### Task 1: 收敛消息契约模型（schema 与协议类型）

**Files:**
- Modify: `witty-service/src/api/schemas.py`
- Modify: `witty-service/src/adapter/websocket_protocol.py`
- Test: `witty-service/tests/unit/test_websocket_protocol.py`

- [ ] **Step 1: 先写/改失败测试，锁定契约**

```python
# witty-service/tests/unit/test_websocket_protocol.py

def test_inbound_event_contains_runtime_type_only():
    event: InboundEvent = {
        "type": "message.delta",
        "session_id": "s1",
        "runtime_type": "openclaw",
        "event_id": "e1",
        "ts_ms": 123,
        "payload": {"delta": "hi"},
    }
    assert "runtime_type" in event
    assert "sandbox_type" not in event
```

- [ ] **Step 2: 运行失败用例**

Run: `pytest witty-service/tests/unit/test_websocket_protocol.py -q`
Expected: FAIL（当前实现与目标契约不一致）

- [ ] **Step 3: 最小实现修改**

```python
# witty-service/src/api/schemas.py
class MessageEventsResponse(BaseModel):
    sandbox_type: str
    events: list[dict[str, Any]]
```

```python
# witty-service/src/adapter/websocket_protocol.py
class InboundEvent(TypedDict):
    type: str
    session_id: str
    runtime_type: str
    event_id: str
    ts_ms: int
    payload: dict[str, Any]
```

- [ ] **Step 4: 回归单测**

Run: `pytest witty-service/tests/unit/test_websocket_protocol.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add witty-service/src/api/schemas.py witty-service/src/adapter/websocket_protocol.py witty-service/tests/unit/test_websocket_protocol.py
git commit -m "refactor: align message schemas with runtime envelope contract"
```

### Task 2: WebSocket 客户端去除事件内 sandbox_type 兼容逻辑

**Files:**
- Modify: `witty-service/src/adapter/websocket_client.py`
- Test: `witty-service/tests/unit/test_websocket_client.py`

- [ ] **Step 1: 写失败测试（禁止解析出 sandbox_type）**

```python
# witty-service/tests/unit/test_websocket_client.py

@pytest.mark.asyncio
async def test_recv_event_keeps_upstream_envelope_without_sandbox_type():
    # mock ws raw json contains runtime_type only
    # assert received event has runtime_type and no sandbox_type
    ...
```

- [ ] **Step 2: 运行目标测试，确认失败**

Run: `pytest witty-service/tests/unit/test_websocket_client.py -q`
Expected: FAIL（当前代码仍在填充 sandbox_type）

- [ ] **Step 3: 最小代码实现**

```python
# witty-service/src/adapter/websocket_client.py (recv)
yield InboundEvent(
    type=data["type"],
    session_id=data["session_id"],
    runtime_type=data["runtime_type"],
    event_id=data["event_id"],
    ts_ms=data["ts_ms"],
    payload=data.get("payload", {}),
)
```

- [ ] **Step 4: 回归测试**

Run: `pytest witty-service/tests/unit/test_websocket_client.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add witty-service/src/adapter/websocket_client.py witty-service/tests/unit/test_websocket_client.py
git commit -m "refactor: remove sandbox_type fallback in websocket events"
```

### Task 3: 非流式接口返回顶层 sandbox_type

**Files:**
- Modify: `witty-service/src/application/agent_manager.py`
- Modify: `witty-service/src/api/agents.py`
- Test: `witty-service/tests/unit/test_agent_manager_ws.py`

- [ ] **Step 1: 写失败测试（非流式返回结构）**

```python
# witty-service/tests/unit/test_agent_manager_ws.py

@pytest.mark.asyncio
async def test_send_message_returns_sandbox_type_and_events():
    result = await manager.send_message(agent.id, session.id, "hello")
    assert result["sandbox_type"] == "local_process"
    assert isinstance(result["events"], list)
    assert "runtime_type" in result["events"][0]
```

- [ ] **Step 2: 运行失败测试**

Run: `pytest witty-service/tests/unit/test_agent_manager_ws.py::test_send_message_returns_sandbox_type_and_events -q`
Expected: FAIL（当前 send_message 返回 list）

- [ ] **Step 3: 最小实现修改**

```python
# witty-service/src/application/agent_manager.py
async def send_message(... ) -> dict[str, Any]:
    ...
    return {
        "sandbox_type": agent.sandbox_type,
        "events": events,
    }
```

```python
# witty-service/src/api/agents.py
@router.post(...)
async def send_message(...):
    payload_obj = await manager.send_message(...)
    return MessageEventsResponse(**payload_obj)
```

- [ ] **Step 4: 运行相关测试**

Run: `pytest witty-service/tests/unit/test_agent_manager_ws.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add witty-service/src/application/agent_manager.py witty-service/src/api/agents.py witty-service/tests/unit/test_agent_manager_ws.py
git commit -m "feat: return sandbox_type at top-level for non-stream messages"
```

### Task 4: 新增 SSE 流式接口 `POST /messages/stream`

**Files:**
- Modify: `witty-service/src/application/agent_manager.py`
- Modify: `witty-service/src/api/agents.py`
- Create: `witty-service/tests/unit/test_messages_stream_api.py`
- Create: `witty-service/tests/e2e/test_message_stream_sse.py`

- [ ] **Step 1: 写 API 层失败测试（SSE 路由存在且响应头正确）**

```python
# witty-service/tests/unit/test_messages_stream_api.py

def test_messages_stream_returns_event_stream_content_type(client):
    resp = client.post(
        f"/api/v1/agents/{agent_id}/sessions/{session_id}/messages/stream",
        json={"content": "hello"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
```

- [ ] **Step 2: 写管理层失败测试（流在 message.completed 结束）**

```python
@pytest.mark.asyncio
async def test_send_message_stream_stops_on_message_completed():
    events = []
    async for item in manager.send_message_stream(agent.id, session.id, "hello"):
        events.append(item)
    assert events[-1]["event"]["type"] == "message.completed"
```

- [ ] **Step 3: 运行新增测试，确认失败**

Run: `pytest witty-service/tests/unit/test_messages_stream_api.py -q`
Expected: FAIL（路由未实现）

- [ ] **Step 4: 实现管理层流式方法**

```python
# witty-service/src/application/agent_manager.py
async def send_message_stream(...):
    ...
    async for event in ws_client.recv():
        yield {"sandbox_type": agent.sandbox_type, "event": dict(event)}
        if event["type"] == "message.completed":
            break
```

- [ ] **Step 5: 实现 API 路由与 SSE 序列化**

```python
# witty-service/src/api/agents.py
from fastapi.responses import StreamingResponse

@router.post("/{agent_id}/sessions/{session_id}/messages/stream")
async def stream_message(...):
    async def event_gen():
        async for item in manager.send_message_stream(...):
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
    return StreamingResponse(event_gen(), media_type="text/event-stream")
```

- [ ] **Step 6: 回归测试**

Run: `pytest witty-service/tests/unit/test_messages_stream_api.py witty-service/tests/unit/test_agent_manager_ws.py -q`
Expected: PASS

- [ ] **Step 7: E2E 测试**

Run: `pytest witty-service/tests/e2e/test_message_stream_sse.py -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add witty-service/src/application/agent_manager.py witty-service/src/api/agents.py witty-service/tests/unit/test_messages_stream_api.py witty-service/tests/e2e/test_message_stream_sse.py
git commit -m "feat: add SSE streaming endpoint for message events"
```

### Task 5: 文档与回归验证

**Files:**
- Modify: `witty-service/README.md`
- Modify: `witty-service/docs/superpowers/specs/2026-04-09-witty-service-websocket-adaptor-design.md`

- [ ] **Step 1: 更新 API 文档**

```markdown
- POST /messages: 非流式，返回 {sandbox_type, events}
- POST /messages/stream: SSE，逐条 data 返回 {sandbox_type, event}
- events 内不再包含 sandbox_type
```

- [ ] **Step 2: 全量相关测试**

Run:
`pytest witty-service/tests/unit/test_websocket_protocol.py witty-service/tests/unit/test_websocket_client.py witty-service/tests/unit/test_agent_manager_ws.py witty-service/tests/unit/test_messages_stream_api.py witty-service/tests/e2e/test_websocket_adaptor.py witty-service/tests/e2e/test_message_stream_sse.py -q`

Expected: PASS

- [ ] **Step 3: 静态检查（仅本模块）**

Run:
`ruff check witty-service/src witty-service/tests`

Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add witty-service/README.md witty-service/docs/superpowers/specs/2026-04-09-witty-service-websocket-adaptor-design.md
git commit -m "docs: document non-stream and SSE message APIs"
```

### Task 6: 发布前验收与风险兜底

**Files:**
- Modify: `witty-service/docs/superpowers/plans/2026-04-13-witty-service-message-sse-streaming-plan.md` (勾选执行结果)

- [ ] **Step 1: 手工 smoke 测试非流式接口**

Run:
`curl -s -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/sessions/${SESSION_ID}/messages" -H 'content-type: application/json' -d '{"content":"hello"}' | jq`

Expected: 返回顶层 `sandbox_type`，`events[]` 事件含 `runtime_type`。

- [ ] **Step 2: 手工 smoke 测试 SSE 接口**

Run:
`curl -N -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/sessions/${SESSION_ID}/messages/stream" -H 'content-type: application/json' -d '{"content":"hello"}'`

Expected: 连续 `data:` 事件，最终 `message.completed` 后连接结束。

- [ ] **Step 3: 回滚预案检查**

- 保留旧 `POST /messages` 路由，若 SSE 出现异常可仅回滚 `/messages/stream` 相关提交。
- 若上游 runtime 事件新增字段，默认透传，不需要紧急修复。

- [ ] **Step 4: 最终提交**

```bash
git add -A
git commit -m "chore: finalize message streaming API rollout"
```
