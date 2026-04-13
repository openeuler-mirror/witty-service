# Witty-Service 端到端测试流程

本文档给出当前 `witty-service` 的完整 E2E（End-to-End）测试方式，覆盖：

1. 本地手工联调
2. 自动化 E2E（pytest）
3. 故障排查清单

当前接口基线：
- Agent 生命周期：`/api/v1/agents/*`
- Session：`/api/v1/agents/{agent_id}/sessions/*`
- 消息接口：`/api/v1/agents/{agent_id}/sessions/{session_id}/messages`、`/api/v1/agents/{agent_id}/sessions/{session_id}/messages/stream`
- 健康检查：`/healthz`

## 1. 前置准备

```bash
pip install -e ".[dev]"
```

建议准备：
- `curl`（HTTP 请求）
- WebSocket 客户端（示例里使用 `websocket-client`）

## 2. 启动服务

```bash
uv run uvicorn src.main:create_app --factory --host 0.0.0.0 --port 8000
```

健康检查：

```bash
curl -s http://127.0.0.1:8000/healthz
```

期望返回：

```json
{"status":"ok"}
```

## 3. 接口模型（输入/输出）

### 3.1 通用错误模型

所有业务错误统一返回：

```json
{
  "code": "ERROR_CODE",
  "message": "error message",
  "details": {}
}
```

字段说明：
- `code`: 稳定错误码
- `message`: 人类可读错误信息
- `details`: 可选，结构化错误细节

### 3.2 接口总览

| 接口 | 方法 | 描述 |
|---|---|---|
| `/healthz` | `GET` | 服务存活检查 |
| `/api/v1/agents` | `POST` | 创建 Agent |
| `/api/v1/agents` | `GET` | 列出所有 Agent |
| `/api/v1/agents/{agent_id}` | `GET` | 获取 Agent 详情 |
| `/api/v1/agents/{agent_id}` | `DELETE` | 删除 Agent |
| `/api/v1/agents/{agent_id}/pause` | `POST` | 暂停 Agent |
| `/api/v1/agents/{agent_id}/resume` | `POST` | 恢复 Agent |
| `/api/v1/agents/{agent_id}/sessions` | `GET` | 列出所有会话 |
| `/api/v1/agents/{agent_id}/sessions` | `POST` | 创建会话 |
| `/api/v1/agents/{agent_id}/sessions/{session_id}` | `GET` | 获取会话详情 |
| `/api/v1/agents/{agent_id}/sessions/{session_id}` | `DELETE` | 删除会话 |
| `/api/v1/agents/{agent_id}/sessions/{session_id}/messages` | `POST` | 发送消息 |
| `/api/v1/agents/{agent_id}/sessions/{session_id}/messages/stream` | `POST` | 发送消息并以 SSE 流返回 |

### 3.3 Agent 生命周期接口

#### 1. `POST /api/v1/agents`

- 接口描述：创建新 Agent
- 输入：

```json
{
  "name": "my-agent",
  "sandbox_type": "docker",
  "adapter_type": "openclaw",
  "idle_timeout_seconds": 3600,
  "sandbox_id": null,
  "has_scheduled_tasks": false
}
```

- 输出 `201`：

```json
{
  "id": "agent-uuid",
  "name": "my-agent",
  "sandbox_type": "docker",
  "adapter_type": "openclaw",
  "status": "running",
  "sandbox_id": null,
  "workspace_path": "/path/to/workspace",
  "idle_timeout_seconds": 3600,
  "has_scheduled_tasks": false,
  "created_at": "2026-04-10T12:00:00",
  "updated_at": "2026-04-10T12:00:00",
  "default_session_id": "session-uuid"
}
```

#### 2. `GET /api/v1/agents`

- 接口描述：列出所有 Agent

#### 3. `GET /api/v1/agents/{agent_id}`

- 接口描述：获取 Agent 详情

#### 4. `DELETE /api/v1/agents/{agent_id}`

- 接口描述：删除 Agent
- 输出 `204`

#### 5. `POST /api/v1/agents/{agent_id}/pause`

- 接口描述：暂停 Agent
- 输出 `200`：返回更新后的 Agent

#### 6. `POST /api/v1/agents/{agent_id}/resume`

- 接口描述：恢复 Agent
- 输出 `200`：返回更新后的 Agent

### 3.4 Session 接口

#### 1. `GET /api/v1/agents/{agent_id}/sessions`

- 接口描述：列出 Agent 的所有会话

#### 2. `POST /api/v1/agents/{agent_id}/sessions`

- 接口描述：创建新会话
- 输出 `201`：

```json
{
  "id": "session-uuid",
  "agent_id": "agent-uuid",
  "status": "active",
  "created_at": "2026-04-10T12:00:00",
  "updated_at": "2026-04-10T12:00:00"
}
```

#### 3. `GET /api/v1/agents/{agent_id}/sessions/{session_id}`

- 接口描述：获取会话详情

#### 4. `DELETE /api/v1/agents/{agent_id}/sessions/{session_id}`

- 接口描述：删除会话
- 输出 `204`

### 3.5 消息接口

#### `POST /api/v1/agents/{agent_id}/sessions/{session_id}/messages`

- 接口描述：witty-service 对外通过 REST 发送消息并返回非流式聚合结果；内部到 `witty-agent-server` 的消息通道仍是 WebSocket
- 输入：

```json
{
  "content": "帮我查一下最近的错误日志"
}
```

- 输出 `200`（`MessageEventsResponse`）：

```json
{
  "sandbox_type": "local_process",
  "events": [
    {
      "type": "message.delta",
      "session_id": "session-id",
      "event_id": "uuid",
      "ts_ms": 1775650000123,
      "runtime_type": "openclaw",
      "payload": {"delta": "当"}
    },
    {
      "type": "message.delta",
      "session_id": "session-id",
      "event_id": "uuid",
      "ts_ms": 1775650000123,
      "runtime_type": "openclaw",
      "payload": {"delta": "前环境"}
    },
    {
      "type": "message.completed",
      "session_id": "session-id",
      "event_id": "uuid",
      "ts_ms": 1775650000123,
      "runtime_type": "openclaw",
      "payload": {"text": "当前工作环境..."}
    }
  ]
}
```

- 说明：
  - 顶层 `sandbox_type` 表示 Agent 的沙箱类型，取值来自 agent 配置的 sandbox backend，例如 `docker`、`local_process`、`e2b`。
  - `events[]` 内不再包含 `sandbox_type`；事件对象保持上游 envelope 结构，字段会保留上游 runtime 的 `runtime_type`，例如 `openclaw`、`opencode`。

#### `POST /api/v1/agents/{agent_id}/sessions/{session_id}/messages/stream`

- 接口描述：witty-service 对外通过 REST 提供 SSE 流式返回；内部到 `witty-agent-server` 的消息通道仍是 WebSocket
- 响应类型：`text/event-stream`
- 每条 SSE `data:` 的格式：

```text
data: {"sandbox_type":"local_process","event":{"type":"message.delta","session_id":"session-id","event_id":"uuid","ts_ms":1775650000123,"runtime_type":"openclaw","payload":{"delta":"当"}}}
```

- 说明：
  - SSE 中每条 `data:` 对应一个上游事件。
  - `sandbox_type` 取自 Agent 的沙箱类型。
  - `event` 的内容保持上游 envelope 结构，包含来自上游 runtime 的 `runtime_type`，不额外嵌套 `sandbox_type`。

### 3.6 事件类型

| type | 含义 | payload 关键字段 |
|------|------|------------------|
| `message.delta` | assistant 增量输出 | `delta` |
| `message.completed` | assistant 输出完成 | `text` |
| `tool.call.started` | 工具调用开始 | `tool_name`, `tool_call_id`, `arguments`, `stage` |
| `tool.call.response` | 工具调用结果/过程输出 | `tool_name`, `tool_call_id`, `content`, `is_error`, `stage` |
| `usage.updated` | 用量更新 | `input_tokens`, `output_tokens`, `total_cost` |
| `session.runtime.changed` | runtime session 标识变化 | runtime 原始字段 |
| `stream.error` | 运行时流异常 | `code`, `message` |
| `client.error` | 客户端事件错误 | `code`, `message`, `details` |

### 3.7 常见错误码

| code | HTTP | 说明 |
|------|------|------|
| `INVALID_AGENT_TRANSITION` | 409 | Agent 状态转换不合法 |
| `SESSION_NOT_FOUND` | 404 | 会话不存在 |
| `AGENT_NOT_RUNNING` | 400 | Agent 未运行 |
| `SANDBOX_UNAVAILABLE` | 503 | sandbox 不可用 |
| `INVALID_MESSAGE_PAYLOAD` | 400 | 消息体不合法 |
| `UNSUPPORTED_CLIENT_EVENT` | 400 | 不支持的客户端事件 |
| `REQUEST_VALIDATION_ERROR` | 422 | 请求验证失败 |

## 4. 手工 E2E 流程

### 4.1 创建 Agent

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' \
  -d '{
    "name": "test-agent",
    "sandbox_type": "docker",
    "adapter_type": "openclaw",
    "idle_timeout_seconds": 3600
  }'
```

### 4.2 创建 Session

```bash
AGENT_ID="your-agent-id"
SESSION_ID=$(
  curl -s -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/sessions" \
    -H 'content-type: application/json' \
    -d '{}' | jq -r '.id'
)
```

### 4.3 发送消息

```bash
AGENT_ID="your-agent-id"
SESSION_ID="your-session-id"

curl -s -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/sessions/${SESSION_ID}/messages" \
  -H 'content-type: application/json' \
  -d '{"content": "say hi"}' | jq
```

### 4.4 暂停/恢复 Agent

```bash
# 暂停
curl -s -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/pause"

# 恢复
curl -s -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/resume"
```

### 4.5 删除 Agent

```bash
curl -s -X DELETE "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}"
```

## 5. Sandbox 类型与配置

### 5.1 docker

在 Docker 容器中运行 adaptor service（`witty-agent-server`）。

**启动行为：**
- 在随机 `host_port` 启动容器
- 将本地 `workspace_path` 挂载到容器的 `/witty-workspace`
- 容器内端口固定为 `8080`

**环境变量：**

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `WITTY_DOCKER_HOST` | Docker 服务监听地址 | `127.0.0.1` |
| `WITTY_DOCKER_IMAGE` | 镜像名（不含 tag） | `witty-agent-server` |
| `WITTY_DOCKER_IMAGE_TAG` | 镜像 tag | `latest` |
| `WITTY_DOCKER_CONTAINER_PORT` | 容器内服务端口 | `8080` |
| `WITTY_DOCKER_CONTAINER_WORKSPACE_PATH` | 容器内工作区路径 | `/witty-workspace` |
| `WITTY_DOCKER_STOP_TIMEOUT` | 容器停止超时（秒） | `10` |

**示例：**
```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' \
  -d '{
    "name": "my-agent",
    "sandbox_type": "docker",
    "adapter_type": "openclaw",
    "idle_timeout_seconds": 3600
  }'
```

**注意事项：**
- `workspace_path` 必须为绝对路径
- 工作区目录必须存在

---

### 5.2 local_process

在本地进程中直接启动 `witty-agent-server`（通过 `uvicorn`）。

**环境变量：**

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `WITTY_AGENT_SERVER_APP_DIR` | `witty-agent-server` 代码目录（必填） | 空 |

**示例：**
```bash
export WITTY_AGENT_SERVER_APP_DIR=/path/to/witty-agent-server

curl -s -X POST http://127.0.0.1:8000/api/v1/agents \
  -H 'content-type: application/json' \
  -d '{
    "name": "my-agent",
    "sandbox_type": "local_process",
    "adapter_type": "openclaw",
    "idle_timeout_seconds": 3600
  }'
```

**注意事项：**
- `WITTY_AGENT_SERVER_APP_DIR` 必须指向有效的 `witty-agent-server` 代码目录

---

### 5.3 e2b

E2B 云沙箱运行时。**当前未实现**，调用会返回 `SANDBOX_NOT_SUPPORTED` 错误。

---

### 5.4 运行时 endpoint

所有 sandbox 启动后，会生成 `AdapterEndpoint`：

```json
{
  "base_url": "http://127.0.0.1:随机端口",
  "health_url": "http://127.0.0.1:随机端口/v1/ping"
}
```

witty-service 通过 WebSocket 连接至 `base_url` 与 adaptor service 通信。

## 6. 自动化测试

```bash
uv run pytest tests/unit/ -q
uv run pytest tests/e2e/ -q
```

全量测试：

```bash
uv run pytest tests/ -q
```

## 7. 故障排查

- `AGENT_NOT_RUNNING`：Agent 未启动或已暂停，先调用 `/resume`
- `SESSION_NOT_FOUND`：会话不存在，检查 session_id 是否正确
- `SANDBOX_UNAVAILABLE`：sandbox 不可用，检查 adaptor service 是否正常运行
- 消息发送无响应：确认 adaptor service 的 WebSocket 连接正常

## 8. 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `AUTH_TOKEN` | API 认证 token | 空 |
| `WITTY_DOCKER_HOST` | Docker 主机地址 | `127.0.0.1` |
| `WITTY_DOCKER_IMAGE` | Docker 镜像名 | `witty-agent-server` |
| `WITTY_DOCKER_IMAGE_TAG` | Docker 镜像标签 | `latest` |
| `WITTY_DOCKER_CONTAINER_PORT` | 容器端口 | `8080` |
| `WITTY_AGENT_SERVER_APP_DIR` | 本地进程模式 app 目录 | 空 |
