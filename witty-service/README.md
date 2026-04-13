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
  "error": {
    "code": "ERROR_CODE",
    "message": "error message",
    "details": {}
  }
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
- 输入（CreateAgentRequest）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | Agent 名称，最小长度 1 |
| `sandbox_type` | string | 是 | 沙箱类型：`docker`、`local_process`、`e2b` |
| `adapter_type` | string | 是 | 适配器类型：如 `openclaw` |
| `idle_timeout_seconds` | integer | 是 | 空闲超时时间（秒），必须大于 0 |
| `sandbox_id` | string | 否 | 沙箱 ID |
| `has_scheduled_tasks` | boolean | 否 | 是否有定时任务，默认 `false` |

- 输出 `201`（AgentResponse）：

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

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | Agent 唯一标识 |
| `name` | string | Agent 名称 |
| `sandbox_type` | string | 沙箱类型 |
| `adapter_type` | string | 适配器类型 |
| `status` | string | Agent 状态：`running`、`paused`、`stopped` |
| `sandbox_id` | string \| null | 沙箱 ID |
| `workspace_path` | string | 工作区路径 |
| `idle_timeout_seconds` | integer | 空闲超时时间 |
| `has_scheduled_tasks` | boolean | 是否有定时任务 |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |
| `default_session_id` | string \| null | 默认会话 ID |

#### 2. `GET /api/v1/agents`

- 接口描述：列出所有 Agent
- 输入：无
- 输出 `200`：`list[AgentResponse]`

```json
[
  {
    "id": "agent-uuid-1",
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
]
```

#### 3. `GET /api/v1/agents/{agent_id}`

- 接口描述：获取 Agent 详情
- 输入：

| 字段 | 类型 | 位置 | 说明 |
|------|------|------|------|
| `agent_id` | string | path | Agent 唯一标识 |

- 输出 `200`：`AgentResponse`

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

#### 4. `DELETE /api/v1/agents/{agent_id}`

- 接口描述：删除 Agent及其所有关联的沙箱资源、会话和消息记录
- 输入：

| 字段 | 类型 | 位置 | 说明 |
|------|------|------|------|
| `agent_id` | string | path | Agent 唯一标识 |

- 输出 `204`：无返回内容

#### 5. `POST /api/v1/agents/{agent_id}/pause`

- 接口描述：暂停 Agent，保留沙箱状态和所有资源
- 输入：

| 字段 | 类型 | 位置 | 说明 |
|------|------|------|------|
| `agent_id` | string | path | Agent 唯一标识 |

- 输出 `200`：`AgentResponse`（status 变为 `paused`）

```json
{
  "id": "agent-uuid",
  "name": "my-agent",
  "sandbox_type": "docker",
  "adapter_type": "openclaw",
  "status": "paused",
  "sandbox_id": "container-id",
  "workspace_path": "/path/to/workspace",
  "idle_timeout_seconds": 3600,
  "has_scheduled_tasks": false,
  "created_at": "2026-04-10T12:00:00",
  "updated_at": "2026-04-10T12:30:00",
  "default_session_id": "session-uuid"
}
```

#### 6. `POST /api/v1/agents/{agent_id}/resume`

- 接口描述：恢复已暂停的 Agent
- 输入：

| 字段 | 类型 | 位置 | 说明 |
|------|------|------|------|
| `agent_id` | string | path | Agent 唯一标识 |

- 输出 `200`：`AgentResponse`（status 变为 `running`）

```json
{
  "id": "agent-uuid",
  "name": "my-agent",
  "sandbox_type": "docker",
  "adapter_type": "openclaw",
  "status": "running",
  "sandbox_id": "container-id",
  "workspace_path": "/path/to/workspace",
  "idle_timeout_seconds": 3600,
  "has_scheduled_tasks": false,
  "created_at": "2026-04-10T12:00:00",
  "updated_at": "2026-04-10T12:35:00",
  "default_session_id": "session-uuid"
}
```

### 3.4 Session 接口

#### 1. `GET /api/v1/agents/{agent_id}/sessions`

- 接口描述：列出 Agent 的所有会话
- 输出 `200`：`list[SessionResponse]`

#### 2. `POST /api/v1/agents/{agent_id}/sessions`

- 接口描述：创建新会话
- 输入：空对象 `{}`
- 输出 `201`（SessionResponse）：

```json
{
  "id": "session-uuid",
  "agent_id": "agent-uuid",
  "status": "active",
  "created_at": "2026-04-10T12:00:00",
  "updated_at": "2026-04-10T12:00:00"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 会话唯一标识 |
| `agent_id` | string | 所属 Agent ID |
| `status` | string | 会话状态：`active`、`closed` |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

#### 3. `GET /api/v1/agents/{agent_id}/sessions/{session_id}`

- 接口描述：获取会话详情
- 输出 `200`：`SessionResponse`

#### 4. `DELETE /api/v1/agents/{agent_id}/sessions/{session_id}`

- 接口描述：删除会话
- 输出 `204`：无返回内容

### 3.5 消息接口

#### `POST /api/v1/agents/{agent_id}/sessions/{session_id}/messages`

- 接口描述：witty-service 对外通过 REST 发送消息并返回非流式聚合结果；内部到 `witty-agent-server` 的消息通道仍是 WebSocket
- 输入（SendMessageRequest）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | 消息内容，最小长度 1 |

```json
{
  "content": "帮我查一下最近的错误日志"
}
```

- 输出 `200`（MessageEventsResponse）：

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

| 字段 | 类型 | 说明 |
|------|------|------|
| `sandbox_type` | string | Agent 的沙箱类型，取值来自 agent 配置的 sandbox backend，例如 `docker`、`local_process`、`e2b` |
| `events` | array | 事件数组，每个事件包含 type、session_id、event_id、ts_ms、runtime_type、payload |

#### `POST /api/v1/agents/{agent_id}/sessions/{session_id}/messages/stream`

- 接口描述：witty-service 对外通过 REST 提供 SSE 流式返回；内部到 `witty-agent-server` 的消息通道仍是 WebSocket
- 输入（SendMessageRequest）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | 消息内容，最小长度 1 |

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

| code | HTTP 状态码 | 说明 |
|------|-------------|------|
| `INVALID_AGENT_TRANSITION` | 409 | Agent 状态转换不合法 |
| `AGENT_NOT_FOUND` | 404 | Agent 不存在 |
| `SESSION_NOT_FOUND` | 404 | 会话不存在 |
| `SESSION_AGENT_MISMATCH` | 400 | Session 与 Agent 不匹配 |
| `AGENT_NOT_RUNNING` | 409 | Agent 未运行（可能处于 paused 或 stopped 状态） |
| `SANDBOX_STATE_NOT_FOUND` | 404 | 沙箱状态不存在 |
| `AGENT_CREATE_FAILED` | 500 | Agent 创建失败 |
| `AGENT_PAUSE_FAILED` | 500 | Agent 暂停失败 |
| `AGENT_RESUME_FAILED` | 500 | Agent 恢复失败 |
| `AGENT_DELETE_FAILED` | 500 | Agent 删除失败 |

**HTTP 状态码映射规则：**
- 以 `_NOT_FOUND` 结尾 → `404`
- 以 `_NOT_SUPPORTED` 或 `_MISMATCH` 结尾 → `400`
- 以 `INVALID_` 开头 → `409`
- 以 `_FAILED` 结尾 → `500`
- 其他 → `400`

## 4. 手工 E2E 流程（按沙箱场景）

### 4.1 Docker 场景

创建 Agent（`sandbox_type=docker`）：

```bash
AGENT_ID=$(
  curl -s -X POST http://127.0.0.1:8000/api/v1/agents \
    -H 'content-type: application/json' \
    -H 'authorization: Bearer YOUR_TOKEN' \
    -d '{
      "name": "e2e-docker-agent",
      "sandbox_type": "docker",
      "adapter_type": "openclaw",
      "idle_timeout_seconds": 3600
    }' | jq -r '.id'
)
```

创建 Session：

```bash
SESSION_ID=$(
  curl -s -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/sessions" \
    -H 'content-type: application/json' \
    -H 'authorization: Bearer YOUR_TOKEN' \
    -d '{}' | jq -r '.id'
)
```

非流式消息接口（`/messages`）：

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/sessions/${SESSION_ID}/messages" \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer YOUR_TOKEN' \
  -d '{"content": "say hi from docker"}' | jq
```

流式消息接口（`/messages/stream`）：

```bash
curl -N -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/sessions/${SESSION_ID}/messages/stream" \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer YOUR_TOKEN' \
  -d '{"content": "stream hi from docker"}'
```

清理：

```bash
curl -s -X DELETE "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}" \
  -H 'authorization: Bearer YOUR_TOKEN'
```

### 4.2 Local Process 场景

先配置本地 `witty-agent-server` 代码目录：

```bash
export WITTY_AGENT_SERVER_APP_DIR=/path/to/witty-agent-server
```

创建 Agent（`sandbox_type=local_process`）：

```bash
AGENT_ID=$(
  curl -s -X POST http://127.0.0.1:8000/api/v1/agents \
    -H 'content-type: application/json' \
    -H 'authorization: Bearer YOUR_TOKEN' \
    -d '{
      "name": "e2e-local-agent",
      "sandbox_type": "local_process",
      "adapter_type": "openclaw",
      "idle_timeout_seconds": 3600
    }' | jq -r '.id'
)
```

创建 Session：

```bash
SESSION_ID=$(
  curl -s -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/sessions" \
    -H 'content-type: application/json' \
    -H 'authorization: Bearer YOUR_TOKEN' \
    -d '{}' | jq -r '.id'
)
```

非流式消息接口（`/messages`）：

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/sessions/${SESSION_ID}/messages" \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer YOUR_TOKEN' \
  -d '{"content": "say hi from local"}' | jq
```

流式消息接口（`/messages/stream`）：

```bash
curl -N -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/sessions/${SESSION_ID}/messages/stream" \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer YOUR_TOKEN' \
  -d '{"content": "stream hi from local"}'
```

可选状态操作：

```bash
curl -s -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/pause" \
  -H 'authorization: Bearer YOUR_TOKEN'
curl -s -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/resume" \
  -H 'authorization: Bearer YOUR_TOKEN'
```

清理：

```bash
curl -s -X DELETE "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}" \
  -H 'authorization: Bearer YOUR_TOKEN'
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

## 6. 认证

所有 `/api/v1/agents/*` 接口需要 Bearer Token 认证：

```bash
-H 'authorization: Bearer YOUR_TOKEN'
```

环境变量：

| 变量名 | 说明 |
|--------|------|
| `AUTH_TOKEN` | API 认证 token |

## 7. 自动化测试

```bash
uv run pytest tests/unit/ -q
uv run pytest tests/e2e/ -q
```

全量测试：

```bash
uv run pytest tests/ -q
```

## 8. 故障排查

- `AGENT_NOT_FOUND`：Agent 不存在，检查 agent_id 是否正确
- `SESSION_NOT_FOUND`：会话不存在，检查 session_id 是否正确
- `SESSION_AGENT_MISMATCH`：Session 与 Agent 不匹配，确认 session 属于正确的 Agent
- `AGENT_NOT_RUNNING`：Agent 未运行（可能处于 paused 或 stopped 状态），先调用 `/resume`
- `AGENT_CREATE_FAILED`：Agent 创建失败，检查 sandbox 配置是否正确
- 消息发送无响应：确认 adaptor service 的 WebSocket 连接正常

## 9. 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `AUTH_TOKEN` | API 认证 token | 空 |
| `WITTY_DOCKER_HOST` | Docker 主机地址 | `127.0.0.1` |
| `WITTY_DOCKER_IMAGE` | Docker 镜像名 | `witty-agent-server` |
| `WITTY_DOCKER_IMAGE_TAG` | Docker 镜像标签 | `latest` |
| `WITTY_DOCKER_CONTAINER_PORT` | 容器端口 | `8080` |
| `WITTY_AGENT_SERVER_APP_DIR` | 本地进程模式 app 目录 | 空 |
