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

## 1. 开发环境设置

### 1.1 创建虚拟环境（如果还没有）

```bash
uv venv
source .venv/bin/activate
```

### 1.2 安装依赖（包含开发依赖）

```bash
uv pip install -e ".[dev]"
```

### 1.3 启动开发服务器

```bash
uv run uvicorn src.main:create_app --factory --host 0.0.0.0 --port 8000
```

建议准备：
- `curl`（HTTP 请求）
- WebSocket 客户端（示例里使用 `websocket-client`）

## 2. 构建与部署

### 2.1 构建 pip 包

```bash
uv build
```

构建产物会生成在 `dist/` 目录下：
- `witty_service-0.1.0-py3-none-any.whl` - Wheel 包
- `witty_service-0.1.0.tar.gz` - Source 包

### 2.2 安装包

```bash
uv pip install dist/witty_service-0.1.0-py3-none-any.whl
```

### 2.3 生产环境启动

```bash
uv run witty-service --host 0.0.0.0 --port 8000 --workers 4
```

### 2.4 启动参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--host` | 绑定的主机地址 | 127.0.0.1 |
| `--port` | 绑定的端口 | 8000 |
| `--log-level` | 日志级别 | info |
| `--reload` | 开发模式自动重载 | False |
| `--workers` | 工作进程数 | 1 |

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
| `/api/v1/models` | `POST` | 添加大模型配置 |
| `/api/v1/models` | `GET` | 获取大模型列表 |
| `/api/v1/models/{model_id}` | `DELETE` | 删除大模型配置 |
| `/api/v1/agents/{agent_id}/sessions/{session_id}/events` | `GET` | 查询会话事件回放 |

说明：
- `GET /api/v1/agents/{agent_id}/sessions`
- `POST /api/v1/agents/{agent_id}/sessions`
- `GET /api/v1/agents/{agent_id}/sessions/{session_id}`
- `DELETE /api/v1/agents/{agent_id}/sessions/{session_id}`
- `GET /api/v1/agents/{agent_id}/sessions/{session_id}/events`

以上接口均支持可选 query 参数 `runtime_agent_id`，用于指定远端 `witty-agent-server` 中的 runtime agent（例如 OpenClaw subagent）。
未显式传入时，`witty-service` 会调用远端 `/agent/list`，优先使用 `defaultId`，若不存在则回退到 `default=true` 的条目。

Agent 接口与 `runtime_agent_id` 的关系：
- `witty-service` 自己的 `agent_id` 表示一个沙箱内的 `witty-agent-server` 进程实例
- 远端 `runtime_agent_id` 表示该进程内的 runtime agent / OpenClaw subagent
- `POST /api/v1/agents`、`GET /api/v1/agents`、`GET /api/v1/agents/{agent_id}`、`DELETE /api/v1/agents/{agent_id}`、`POST /api/v1/agents/{agent_id}/pause`、`POST /api/v1/agents/{agent_id}/resume` 这些 Agent 生命周期接口**不直接接收** `runtime_agent_id`
- `runtime_agent_id` 只影响 session 相关接口的远端路由选择，不改变 `witty-service` 自己的 agent 主键语义

### 3.3 Agent 生命周期接口

#### 1. `POST /api/v1/agents`

- 接口描述：创建新 Agent
- 说明：
  - 该接口不接收 `runtime_agent_id`
  - 创建完成后，`witty-service` 会先调用远端 `/agent/start`
  - 然后使用 `/agent/start` 返回的远端 runtime agent id 创建默认 session
  - 因此 `default_session_id` 对应的远端归属由 `witty-agent-server` 当前解析出的默认/启动目标 agent 决定
- 输入（CreateAgentRequest）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | Agent 名称，最小长度 1 |
| `description` | string | 否 | Agent 描述，默认空字符串 |
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
  "description": "这是一个测试智能体",
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
| `description` | string | Agent 描述 |
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

补充说明：
- `default_session_id` 是 Agent 创建成功后立即创建的默认 session
- 这个默认 session 的远端 `runtime_agent_id` 不是通过 `POST /api/v1/agents` 入参传入，而是由内部 `/agent/start` 结果决定
- 如果你需要把后续 session 显式路由到某个 remote runtime agent（例如 `dev`），应在 `POST /api/v1/agents/{agent_id}/sessions?runtime_agent_id=dev` 时传入

#### 2. `GET /api/v1/agents`

- 接口描述：列出所有 Agent
- 输入：无
- 输出 `200`：`list[AgentResponse]`

```json
[
  {
    "id": "agent-uuid-1",
    "name": "my-agent",
    "description": "这是一个测试智能体",
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
  "description": "这是一个测试智能体",
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

- 接口描述：删除 Agent - 备份运行时 → 停止运行时 → 清理沙箱 → 删除本地记录
- 输入：

| 字段 | 类型 | 位置 | 说明 |
|------|------|------|------|
| `agent_id` | string | path | Agent 唯一标识 |

- 输出 `204`：无返回内容

- 说明：
  1. 备份运行时文件到 `~/witty-service/{agent_id}/runtime_backup/`
  2. 调用 witty-agent-server `/agent/stop`
  3. 清理沙箱（docker stop/rm 或 kill process）
  4. 更新 agent 状态为 deleted
  5. **保留 workspace 目录**（不清除，用于后续 resume）

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

- 接口描述：恢复已暂停的 Agent，根据状态分支处理
- 输入：

| 字段 | 类型 | 位置 | 说明 |
|------|------|------|------|
| `agent_id` | string | path | Agent 唯一标识 |

- 输出 `200`：`AgentResponse`（status 变为 `running`）

- 分支逻辑：

| 当前状态 | 恢复流程 |
|----------|----------|
| `paused` | 直接调用 `/agent/start` |
| `deleted` / `stopped` | 1. 恢复运行时备份<br>2. 重新启动沙箱<br>3. 调用 `/agent/start` |

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

- 接口描述：列出 Agent 的所有会话（以 witty-agent-server 为主，刷新本地缓存）
- 输出 `200`：`list[SessionResponse]`

#### 2. `POST /api/v1/agents/{agent_id}/sessions`

- 接口描述：创建新会话（透传到 witty-agent-server）
- 输入：空对象 `{}`
- 输出 `201`（SessionResponse）：

```json
{
  "id": "session-uuid",
  "agent_id": "agent-uuid",
  "status": "idle",
  "context_initialized": true,
  "runtime_type": "openclaw",
  "created_at": "2026-04-10T12:00:00",
  "updated_at": "2026-04-10T12:00:00"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 会话唯一标识 |
| `agent_id` | string | 所属 Agent ID |
| `status` | string | 会话运行态：`running`、`idle`、`error` |
| `context_initialized` | boolean | witty-agent-server 是否已完成上下文初始化 |
| `runtime_type` | string | 运行时类型：如 `openclaw` |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

- 额外说明：
  - `runtime_agent_id` 是创建前的路由参数。
  - 创建成功后，`witty-service` 会在本地固化 `remote_runtime_agent_id`，后续该 session 的查询、删除、事件回放、消息发送和 WS 连接都会优先使用这个固化值。
  - session 生命周期不再通过 `deleted`、`closed` 之类的状态表达，而是通过“记录是否存在 + 接口行为结果”体现。

#### 3. `GET /api/v1/agents/{agent_id}/sessions/{session_id}`

- 接口描述：获取会话详情（优先本地，透传 witty-agent-server）
- 输出 `200`：`SessionResponse`

#### 4. `DELETE /api/v1/agents/{agent_id}/sessions/{session_id}`

- 接口描述：删除会话（透传到 witty-agent-server，删除本地记录）
- 输出 `204`：无返回内容
- 说明：内部会调用远端 `POST /agents/{remote_runtime_agent_id}/sessions/{session_id}/delete`

#### 5. `GET /api/v1/agents/{agent_id}/sessions/{session_id}/events`

- 接口描述：查询会话事件回放（透传到 witty-agent-server）
- 输入：

| 字段 | 类型 | 位置 | 说明 |
|------|------|------|------|
| `session_id` | string | path | 会话 ID |
| `offset` | integer | query | 分页偏移，默认 0 |
| `limit` | integer | query | 每页数量，默认 50 |

- 输出 `200`（SessionEventPage）：

```json
{
  "items": [
    {
      "id": "event-id",
      "session_id": "session-id",
      "type": "message.delta",
      "source": "assistant",
      "payload": {},
      "timestamp": "2026-03-31T12:34:56.000000Z"
    }
  ],
  "pagination": {
    "offset": 0,
    "limit": 50,
    "total": 1
  }
}
```

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
  - session 状态同步规则为：收到运行中事件时更新为 `running`，收到 `message.completed` 后收敛为 `idle`，收到 `client.error` / `stream.error` 或 WS 异常时更新为 `error`。

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

### 3.6 大模型配置接口

#### 1. `POST /api/v1/models`

- 接口描述：添加新的大模型配置
- 输入（CreateModelRequest）：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 模型名称，如 `gpt-4o`、`claude-3-opus-20240229` |
| `provider` | string | 是 | 模型提供商：`openai`、`anthropic`、`google`、`ollama`、`azure`、`deepseek`、`glm`、`minimax`、`kimi`、`custom` |
| `api_key` | string | 是 | API 密钥 |
| `api_base_url` | string | 否 | API 基础地址，默认根据 provider 自动设置 |
| `description` | string | 否 | 模型描述，默认空字符串 |
| `enabled` | boolean | 否 | 是否启用，默认 `true` |
| `max_tokens` | integer | 否 | 最大输出 tokens 数，默认 4096 |
| `temperature` | number | 否 | 生成温度，范围 0-2，默认 0.7 |
| `is_default` | boolean | 否 | 是否设为默认模型，默认 `false` |

- 输出 `201`（ModelResponse）：

```json
{
  "id": "model-uuid",
  "name": "gpt-4o",
  "provider": "openai",
  "api_base_url": "https://api.openai.com/v1",
  "description": "OpenAI GPT-4 Omni 模型",
  "enabled": true,
  "max_tokens": 4096,
  "temperature": 0.7,
  "is_default": true,
  "created_at": "2026-04-13T10:00:00",
  "updated_at": "2026-04-13T10:00:00"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 模型配置唯一标识 |
| `name` | string | 模型名称 |
| `provider` | string | 模型提供商 |
| `api_base_url` | string | API 基础地址 |
| `description` | string | 模型描述 |
| `enabled` | boolean | 是否启用 |
| `max_tokens` | integer | 最大输出 tokens 数 |
| `temperature` | number | 生成温度 |
| `is_default` | boolean | 是否为默认模型 |
| `created_at` | datetime | 创建时间 |
| `updated_at` | datetime | 更新时间 |

#### 2. `GET /api/v1/models`

- 接口描述：获取所有大模型配置列表
- 输出 `200`：`list[ModelResponse]`

```json
[
  {
    "id": "model-uuid-1",
    "name": "gpt-4o",
    "provider": "openai",
    "api_base_url": "https://api.openai.com/v1",
    "description": "OpenAI GPT-4 Omni 模型",
    "enabled": true,
    "max_tokens": 4096,
    "temperature": 0.7,
    "is_default": true,
    "created_at": "2026-04-13T10:00:00",
    "updated_at": "2026-04-13T10:00:00"
  },
  {
    "id": "model-uuid-2",
    "name": "claude-3-opus-20240229",
    "provider": "anthropic",
    "api_base_url": "https://api.anthropic.com/v1",
    "description": "Anthropic Claude 3 Opus 模型",
    "enabled": true,
    "max_tokens": 4096,
    "temperature": 0.7,
    "is_default": false,
    "created_at": "2026-04-13T11:00:00",
    "updated_at": "2026-04-13T11:00:00"
  }
]
```

#### 3. `DELETE /api/v1/models/{model_id}`

- 接口描述：删除指定的大模型配置
- 输入：

| 字段 | 类型 | 位置 | 说明 |
|------|------|------|------|
| `model_id` | string | path | 模型配置唯一标识 |

- 输出 `204`：无返回内容

**支持的模型提供商及默认 API 地址：**

| Provider | 默认 API Base URL |
|----------|-------------------|
| `openai` | `https://api.openai.com/v1` |
| `anthropic` | `https://api.anthropic.com/v1` |
| `google` | `https://generativelanguage.googleapis.com/v1beta` |
| `ollama` | `http://localhost:11434/v1` |
| `azure` | `https://{resource}.openai.azure.com` |
| `deepseek` | `https://api.deepseek.com/v1` |
| `glm` | `https://open.bigmodel.cn/api/paas/v4` |
| `minimax` | `https://api.minimax.chat/v1` |
| `kimi` | `https://api.moonshot.cn/v1` |
| `custom` | 用户自定义，需通过 `api_base_url` 指定 |

**常见错误码：**

| code | HTTP 状态码 | 说明 |
|------|-------------|------|
| `MODEL_NOT_FOUND` | 404 | 模型配置不存在 |
| `MODEL_DUPLICATE` | 409 | 模型配置已存在 |
| `MODEL_CREATE_FAILED` | 500 | 模型配置创建失败 |
| `MODEL_DELETE_FAILED` | 500 | 模型配置删除失败 |

### 3.7 常见错误码

| code | HTTP 状态码 | 说明 |
|------|-------------|------|
| `INVALID_AGENT_TRANSITION` | 409 | Agent 状态转换不合法 |
| `AGENT_NOT_FOUND` | 404 | Agent 不存在 |
| `SESSION_NOT_FOUND` | 404 | 会话不存在（两边都查不到） |
| `SESSION_CREATE_FAILED` | 500 | 透传创建会话失败 |
| `SESSION_DELETE_FAILED` | 500 | 透传删除会话失败 |
| `SESSION_LIST_FAILED` | 500 | 透传列出会话失败 |
| `SESSION_AGENT_MISMATCH` | 400 | Session 与 Agent 不匹配 |
| `AGENT_NOT_RUNNING` | 409 | Agent 未运行（可能处于 paused 或 stopped 状态） |
| `SANDBOX_STATE_NOT_FOUND` | 404 | 沙箱状态不存在 |
| `AGENT_CREATE_FAILED` | 500 | Agent 创建失败 |
| `AGENT_PAUSE_FAILED` | 500 | Agent 暂停失败 |
| `AGENT_RESUME_FAILED` | 500 | Agent 恢复失败 |
| `AGENT_DELETE_FAILED` | 500 | Agent 删除失败 |
| `RUNTIME_BACKUP_NOT_FOUND` | 404 | 运行时备份不存在 |
| `RUNTIME_BACKUP_RESTORE_FAILED` | 500 | 恢复备份失败 |
| `RUNTIME_START_FAILED` | 500 | 启动运行时失败 |

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
      "description": "Docker 场景测试智能体",
      "sandbox_type": "docker",
      "adapter_type": "openclaw",
      "idle_timeout_seconds": 3600
    }' | jq -r '.id'
)
```

创建 Session：

```bash
SESSION_ID=$(
  curl -s -X POST "http://127.0.0.1:8000/api/v1/agents/${AGENT_ID}/sessions?runtime_agent_id=dev" \
    -H 'content-type: application/json' \
    -H 'authorization: Bearer YOUR_TOKEN' \
    -d '{}' | jq -r '.id'
)
```

说明：
- 若不传 `runtime_agent_id`，会使用远端默认 runtime agent。
- 若希望显式打到某个 subagent（例如 `dev`），请通过 query 参数传入。

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
      "description": "Local Process 场景测试智能体",
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
- local process 场景下，每个 `witty-service` agent 都会拉起一个独立的 `witty-agent-server` 进程，并占用一个随机端口
- `witty-service.agent_id` 表示这个本地子进程实例；远端 `runtime_agent_id` 表示该进程内的 OpenClaw subagent，两者不是同一个维度

---

### 5.3 e2b

E2B 云沙箱运行时。**当前未实现**，调用会返回 `SANDBOX_NOT_SUPPORTED` 错误。

---

### 5.4 运行时 endpoint

所有 sandbox 启动后，会生成 `AdapterEndpoint`：

```json
{
  "base_url": "http://127.0.0.1:随机端口",
  "health_url": "http://127.0.0.1:随机端口/ping"
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
- `SESSION_NOT_FOUND`：会话不存在（两边都查不到），检查 session_id 是否正确
- `RUNTIME_AGENT_DEFAULT_NOT_FOUND`：未显式传 `runtime_agent_id`，且远端 `/agent/list` 没有可用默认 agent
- `RUNTIME_AGENT_ID_MISSING`：本地 session 未固化远端 runtime agent id，无法继续路由
- `SESSION_CREATE_FAILED`：创建会话失败，透传到 witty-agent-server 失败，检查 witty-agent-server 是否正常运行
- `SESSION_DELETE_FAILED`：删除会话失败，透传到 witty-agent-server 失败
- `SESSION_LIST_FAILED`：列出会话失败，透传到 witty-agent-server 失败
- `SESSION_AGENT_MISMATCH`：Session 与 Agent 不匹配，确认 session 属于正确的 Agent
- `AGENT_NOT_RUNNING`：Agent 未运行（可能处于 paused 或 stopped 状态），先调用 `/resume`
- `RUNTIME_BACKUP_NOT_FOUND`：运行时备份不存在，可能该 Agent 未执行过 delete 操作
- `RUNTIME_BACKUP_RESTORE_FAILED`：恢复备份失败，检查备份文件是否完整
- 消息发送无响应：确认 witty-agent-server 的 WebSocket 连接正常
- local_process 创建 Agent 返回 500：优先检查 `WITTY_AGENT_SERVER_APP_DIR` 是否正确，以及子进程里的 `witty-agent-server` 是否能正常响应 `/ping`

## 9. 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `AUTH_TOKEN` | API 认证 token | 空 |
| `WITTY_DOCKER_HOST` | Docker 主机地址 | `127.0.0.1` |
| `WITTY_DOCKER_IMAGE` | Docker 镜像名 | `witty-agent-server` |
| `WITTY_DOCKER_IMAGE_TAG` | Docker 镜像标签 | `latest` |
| `WITTY_DOCKER_CONTAINER_PORT` | 容器端口 | `8080` |
| `WITTY_AGENT_SERVER_APP_DIR` | 本地进程模式 app 目录 | 空 |
