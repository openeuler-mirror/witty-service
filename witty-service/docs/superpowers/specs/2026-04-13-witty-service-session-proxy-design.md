# Witty-Service Session Proxy 重构设计文档

- 日期：2026-04-13
- 版本：v3.0
- 状态：待实现
- 替换：v2.2 中的 Session 接口设计

## 1. 背景与目标

### 1.1 当前问题

witty-service 的 Session 接口（创建/查询/删除/事件回放）仅操作本地 SQLite 数据库，没有与 witty-agent-server 交互。这导致：

1. **Session 生命周期不一致**：witty-service 和 witty-agent-server 各管理一套 session
2. **无法查询运行时会话**：witty-service 不知道 witty-agent-server 端的 session 详情
3. **事件回放缺失**：无法从 witty-agent-server 获取历史事件

### 1.2 目标

将 witty-service 重构为 **Session Proxy 模式**：
- witty-agent-server 作为 Session 的主数据源
- witty-service 本地数据库作为缓存
- 不一致时以 witty-agent-server 为准，刷新本地数据库

## 2. 架构设计

### 2.1 核心原则

1. **Session Proxy 模式**：witty-service 作为 proxy，Session 生命周期全部透传到 witty-agent-server
2. **主数据源**：witty-agent-server 是 Session 的主数据源，witty-service 本地数据库作为缓存
3. **缓存一致性**：List/Query 时以 witty-agent-server 为主，不一致时刷新本地数据库
4. **优雅操作**：pause 调用 `/agent/stop` 优雅停止，不清理沙箱
5. **运行时备份**：delete 时备份运行时文件到本地，resume 时恢复

### 2.2 目录结构

```
~/witty-service/
├── agent-workspaces/{agent_id}/workspace/    ← workspace（保持不变）
└── {agent_id}/runtime_backup/                ← 运行时备份
    └── .openclaw/                            ← openclaw 运行时备份
```

- `LocalWorkspaceStore` 的 base_path 改为 `~/witty-service/`
- workspace 仍放在 `~/witty-service/agent-workspaces/{agent_id}/workspace/`
- 运行时备份放在 `~/witty-service/{agent_id}/runtime_backup/`

## 3. Session 接口重构

### 3.1 接口映射

| witty-service HTTP API | witty-agent-server 接口 | 说明 |
|------------------------|------------------------|------|
| `POST /api/v1/agents/{agent_id}/sessions` | `POST /agent/sessions` | 创建会话，存储到本地缓存 |
| `GET /api/v1/agents/{agent_id}/sessions` | `GET /agent/sessions` | 以 witty-agent-server 为主，刷新本地缓存 |
| `GET /api/v1/agents/{agent_id}/sessions/{session_id}` | `GET /agent/sessions/{session_id}` | 以 witty-agent-server 为主，存储到本地缓存 |
| `DELETE /api/v1/agents/{agent_id}/sessions/{session_id}` | `DELETE /agent/sessions/{session_id}` | 透传删除，删除本地缓存 |
| `GET /api/v1/agents/{agent_id}/sessions/{session_id}/events` | `GET /agent/sessions/{session_id}/events` | 透传事件回放 |

### 3.2 Session 创建流程

```
POST /api/v1/agents/{agent_id}/sessions
     │
     ▼
1. witty-service 本地验证 agent 存在且状态为 running
     │
     ▼
2. 透传 POST {witty-agent-server}/agent/sessions
     │
     ▼
3. 存储 session 到本地：id, agent_id, status=active, created_at
     │
     ▼
4. 返回 SessionResponse
```

### 3.3 Session 列表流程（witty-agent-server 为主）

```
GET /api/v1/agents/{agent_id}/sessions
     │
     ▼
1. 透传 GET {witty-agent-server}/agent/sessions
     │
     ▼
2. 以返回结果为主数据源
     │
     ▼
3. 刷新本地数据库缓存（upsert）
     │
     ▼
4. 返回 list[SessionResponse]
```

### 3.4 Session 删除流程

```
DELETE /api/v1/agents/{agent_id}/sessions/{session_id}
     │
     ▼
1. 透传到 witty-agent-server 删除会话
     │
     ▼
2. 删除本地 session 记录
     │
     ▼
3. 返回 204
```

## 4. Agent 生命周期重构

### 4.1 Pause 流程（新）

```
POST /api/v1/agents/{agent_id}/pause
     │
     ▼
1. 验证 agent 状态为 running
     │
     ▼
2. 调用 witty-agent-server /agent/stop（优雅停止运行时）
     │
     ▼
3. 更新 agent 状态为 paused
     │
     ▼
4. 保持沙箱运行（不清理 docker 容器或 subprocess）
     │
     ▼
5. 返回更新后的 AgentResponse
```

### 4.2 Delete 流程（新）

```
DELETE /api/v1/agents/{agent_id}
     │
     ▼
1. 备份运行时文件
   源：~/.openclaw
   目标：~/witty-service/{agent_id}/runtime_backup/.openclaw
     │
     ▼
2. 调用 witty-agent-server /agent/stop（如果运行时还在运行）
     │
     ▼
3. 清理沙箱
   docker: docker stop/rm
   local_process: kill process
     │
     ▼
4. 更新 agent 状态为 deleted
     │
     ▼
5. 保留 workspace 目录（不清除，用于后续 resume）
     │
     ▼
6. 返回 204
```

> **注意**：Delete 后 workspace 目录保留，用于后续 resume 时继续使用。Resume 时：
> - `paused` 状态：直接调 `/agent/start`（workspace 保持不变）
> - `deleted` 状态：恢复运行时备份 → 重新启动沙箱 → 调 `/agent/start`（workspace 保持不变）

### 4.3 Resume 流程（分支逻辑）

```
POST /api/v1/agents/{agent_id}/resume
     │
     ▼
检查 agent 状态：
     │
     ├─── "paused" ────────────────────
     │    │
     │    ▼
     │    1. 验证沙箱是否仍在运行
     │    │
     │    ▼
     │    2. 调用 witty-agent-server /agent/start
     │    │
     │    ▼
     │    3. 更新 agent 状态为 running
     │    │
     │    ▼
     │    4. 返回 AgentResponse
     │
     │
     └─── "deleted" ───────
          │
          ▼
     1. 检查运行时备份是否存在
        ~/witty-service/{agent_id}/runtime_backup/
          │
          ├─── 存在 ──────────────────
          │    ▼
          │    恢复运行时备份
          │    备份目录 ~/.openclaw 覆盖恢复
          │
          ▼
     2. 重新启动沙箱
        docker: docker run（重新创建容器，挂载 workspace）
        local_process: subprocess.Popen
          │
          ▼
     3. 等待沙箱就绪（健康检查）
          │
          ▼
     4. 调用 witty-agent-server /agent/start
          │
          ▼
     5. 更新 agent 状态为 running
          │
          ▼
     6. 返回 AgentResponse
```

#### 4.3.1 deleted 场景详细说明

**前置条件**：
- Agent 状态为 `deleted`
- Workspace 目录保留在 `~/witty-service/agent-workspaces/{agent_id}/workspace/`
- 运行时备份在 `~/witty-service/{agent_id}/runtime_backup/`

**恢复步骤**：

| 步骤 | 操作 | 说明 |
|------|------|------|
| 1 | 检查备份 | 确认 `runtime_backup` 目录存在 |
| 2 | 恢复运行时 | 将 `runtime_backup/.openclaw` 覆盖到 `~/.openclaw` |
| 3 | 启动沙箱 | docker: `docker run` 重新创建容器，挂载 workspace<br>local_process: `subprocess.Popen` 重新启动进程 ，此时agent_id要复用，不能生成新的|
| 4 | 健康检查 | 轮询 `/v1/ping`，确认沙箱就绪 |
| 5 | 启动运行时 | 调用 `/agent/start`，witty-agent-server 初始化运行时 |
| 6 | 更新状态 | 更新 agent 状态为 `running`，sandbox_id 更新 |

**沙箱启动参数**：

| 沙箱类型 | 启动方式 | workspace 挂载 | agent_id |
|----------|----------|----------------|----------|
| `docker` | `docker run` | 挂载 `~/witty-service/agent-workspaces/{agent_id}/workspace` → `~/witty-workspace` | 	复用原有 agent_id |
| `local_process` | `subprocess.Popen` | `cwd=workspace_path`，进程在 workspace 目录下启动 | **复用原有 agent_id** |

**异常处理**：

| 场景 | 处理方式 |
|------|----------|
| 备份不存在 | 直接抛异常 `RUNTIME_BACKUP_NOT_FOUND`, 状态保持 deleted|
| 沙箱启动失败 | 抛出 `SANDBOX_START_FAILED`，状态保持 deleted |
| 健康检查超时 | 抛出 `SANDBOX_NOT_READY`，状态保持 deleted |
| /agent/start 失败 | 抛出 `RUNTIME_START_FAILED`，状态保持 deleted |

**agent_id 复用**：
- `local_process` 场景下，resume 时使用原有的 `agent_id`
- `agent_id` 不变，workspace 目录不变，只重新启动进程和运行时

#### 4.3.2 paused 场景详细说明

**前置条件**：
- Agent 状态为 `paused`
- 沙箱（docker 容器或 subprocess）仍在运行
- 运行时已停止（通过 `/agent/stop`）

**恢复步骤**：

| 步骤 | 操作 | 说明 |
|------|------|------|
| 1 | 验证沙箱 | 检查 docker 容器或进程是否仍在运行 |
| 2 | 健康检查 | 轮询 `/v1/ping`，确认沙箱就绪 |
| 3 | 启动运行时 | 调用 `/agent/start` |
| 4 | 更新状态 | 更新 agent 状态为 `running` |

**异常处理**：

| 场景 | 处理方式 |
|------|----------|
| 沙箱已停止 | 降级到 deleted 场景，按 deleted 流程处理 |
| 健康检查超时 | 抛出 `SANDBOX_NOT_READY`，状态保持 paused |
| /agent/start 失败 | 抛出 `RUNTIME_START_FAILED`，状态保持 paused |

### 4.4 Agent 状态说明

| 状态 | 沙箱 | 运行时 | 说明 |
|------|------|--------|------|
| `creating` | 已创建 | 未启动 | Agent 正在创建 |
| `running` | 运行中 | 运行中 | Agent 正常运行 |
| `paused` | 运行中 | 已停止 | 运行时暂停，沙箱保持 |
| `deleted` | 已清理 | 已停止 | 沙箱已清理，有备份 |
| `error` | 不确定 | 不确定 | 操作失败 |

## 5. 数据模型

### 5.1 SessionResponse 更新

```json
{
  "id": "session-uuid",
  "agent_id": "agent-uuid",
  "status": "active",
  "context_initialized": true,
  "runtime_type": "openclaw",
  "created_at": "2026-04-10T12:00:00",
  "updated_at": "2026-04-10T12:00:00"
}
```

新增字段：
- `context_initialized`: bool - witty-agent-server 创建时返回
- `runtime_type`: string - witty-agent-server 创建时返回

### 5.2 SandboxStateRecord 更新

可能需要新增字段标记：
- `runtime_backup_path`: 运行时备份路径
- `last_shutdown_mode`: "graceful" | "force" | "none"

## 6. 错误处理

### 6.1 Session 相关错误

| 错误码 | HTTP | 说明 |
|--------|------|------|
| `SESSION_NOT_FOUND` | 404 | Session 不存在（两边都查不到） |
| `SESSION_CREATE_FAILED` | 500 | 透传创建失败 |
| `SESSION_DELETE_FAILED` | 500 | 透传删除失败 |
| `SESSION_LIST_FAILED` | 500 | 透传列表失败 |
| `SESSION_AGENT_MISMATCH` | 400 | Session 不属于该 Agent |

### 6.2 Resume 相关错误

| 错误码 | HTTP | 说明 |
|--------|------|------|
| `RUNTIME_BACKUP_NOT_FOUND` | 404 | 运行时备份不存在 |
| `RUNTIME_BACKUP_RESTORE_FAILED` | 500 | 恢复备份失败 |
| `RUNTIME_START_FAILED` | 500 | 启动运行时失败 |

## 7. 变更文件清单

### 7.1 核心修改

| 文件 | 变更内容 |
|------|----------|
| `src/application/session_manager.py` | 透传到 witty-agent-server |
| `src/application/agent_manager.py` | Pause/Resume/Delete 流程改造 |
| `src/storage/workspace_store.py` | base_path 改为 ~/witty-service/ |
| `src/api/agents.py` | Session 路由调整 |
| `src/api/schemas.py` | SessionResponse 新增字段 |
| `src/persistence/repositories.py` | Session 持久化调整 |

### 7.2 新增文件

| 文件 | 说明 |
|------|------|
| `src/storage/runtime_backup.py` | 运行时备份/恢复逻辑 |
| `src/adapter/http_client.py` | HTTP 客户端（调用 witty-agent-server API） |

### 7.3 文档更新

| 文件 | 说明 |
|------|------|
| `docs/superpowers/specs/2026-04-09-witty-service-websocket-adaptor-design.md` | 更新架构设计 |
| `README.md` | 更新接口说明 |

## 8. 实现顺序

### Phase 1: 基础设施
1. 新增 `RuntimeBackupStore` - 运行时备份/恢复
2. 新增 `AdaptorHttpClient` - HTTP 客户端封装
3. 修改 `LocalWorkspaceStore` base_path

### Phase 2: Session 接口
4. 修改 `SessionManager` - 透传创建/查询/删除
5. 修改 Session API 路由
6. 更新 SessionResponse schema

### Phase 3: Agent 生命周期
7. 修改 `pause_agent` - 只调 /agent/stop
8. 修改 `delete_agent` - 备份 + 清理
9. 修改 `resume_agent` - 分支逻辑

### Phase 4: 测试与文档
10. 单元测试
11. E2E 测试
12. 文档更新
