# ADR 0002: Workspace Runtime 落在控制面并以 Port 隔离执行适配层

- 状态：Accepted
- 日期：2026-11-02（2026-11-08 按 C01 修订）
- 前置决策：[ADR 0001](./0001-workspace-scoped-opencode-runtime.md)

## Context

ADR 0001 已确定每个活动 Workspace 至多一个 OpenCode Server，但没有确定 Server 由谁启动和持有、配置投影与事件流由谁编排、控制面与执行适配层和沙箱之间允许怎样的依赖。

现状与目标存在四处结构性冲突：

1. 一个 Agent 一个 `witty_agent_server` HTTP 进程，进程内再启动 `opencode serve`；切换 Agent 会换进程、换目录、换上下文。
2. 控制面通过 `witty_service/adapter` 的 HTTP/WS 调用 agent-server；每个会话一条 WS，事件、终态和取消语义散落在 WS 路由里。
3. sandbox 以 Agent 为 owner，句柄、端口、进程组和清理都以 `agent_id` 为键。
4. agent-server 拥有 Agent/Session/Skill/MCP 的装配与 HTTP 入口，控制面无法独占领域规则。

首版只有 OpenCode 一种 Runtime。需要固定的是依赖方向和职责，不是 `runtime_type` 分支或通用插件层。C04 的 Port/DTO 已按本 ADR 落位为 Draft；本 ADR 不冻结签名，只冻结落位、职责、调用方向、删除清单和 M1 最薄链路。

## Decision

### 1. 落位与依赖方向

- `Workspace Runtime Manager` 位于控制面进程内（`witty_service.runtime`），与 API、Session Service、Turn Service 同进程。单控制面实例下内存状态即权威，不建 Runtime Server 表。
- 执行适配层是库，不是服务：`witty_agent_server` 收窄为 OpenCode 协议适配与事件归一化。P0 不启动 `witty_agent_server.app:create_app`，也不保留其 Agent/Session HTTP 入口。
- 沙箱为每个活动 Workspace 直接拉起一个 `opencode serve` 进程（`local_process`）或一个容器（`docker`）。这是 P0 唯一被管理的执行进程，进程内不再嵌套第二个 HTTP 服务。
- 组合根是唯一注入点：计划新增 `witty_service/bootstrap.py`（R01），只 import `witty_agent_server.runtime_adapter` 暴露的工厂，把 `WorkspaceRuntimePort` 实现注入 Manager。其余 `witty_service` 模块不得 import `witty_agent_server`。
- 依赖方向固定为 `witty_agent_server → witty_service`。adapter 只可导入契约 DTO、`stream_errors`、`usage`、`artifact_detector`、`opencode_mcp_config`、`config`、`domain.errors`；不得 import sandbox、persistence 或领域服务。

| 模块 | 位置 | 允许依赖 |
| --- | --- | --- |
| Manager | `witty_service/runtime/manager.py` | `WorkspaceRuntimePort` 及其 DTO、`SandboxBackend` 及其 handle、投影输入快照 |
| Session/Turn Service、History Projection、Catalog | `witty_service/**` | 自身领域、Manager 的公开方法；不直接接触 Port 或 sandbox |
| 组合根 | `witty_service/bootstrap.py` | `witty_agent_server.runtime_adapter` 的公开工厂 |
| OpenCode 执行适配层 | `witty_agent_server/**` | `witty_service.runtime` 共享模块、`witty_service.config`、`witty_service.domain.errors` |
| Sandbox | `witty_service/sandbox/**` | `witty_service.config`、`witty_service.domain.errors` |

调用方向固定为 `入口（Console/Channel/Scheduler）→ Session/Turn Service → Manager → {WorkspaceRuntimePort, SandboxBackend}`。Runtime Session 的惰性创建、prompt、abort 由 Turn Service 调用 Manager；删除 Session 时由 Session Service 调用 Manager 关闭并删除 Runtime Session；History Projection 经 Manager 读取历史。除这些公开方法外，任何模块不得绕过 Manager 直接调用 Port 或 SandboxBackend。

`bootstrap.py` 不放在 `witty_service.runtime` 包内，避免 adapter 导入 `witty_service.runtime.*` 时经包初始化反向触发组合根。

### 2. 职责矩阵

| 模块 | 负责 | 明确不负责 |
| --- | --- | --- |
| Workspace Runtime Manager | Server 目标状态、启动/停止/重启/空闲回收、配置投影与发布、Turn Permit、单条 SSE 消费与路由、`runtime_status` 读模型、内存注册表 | Turn 归因与终态、消息正文、Agent/Skill/MCP 目录维护 |
| Session Service | 当前 Agent/模型选择、`runtime_session_id`、MCP 激活、删除准入 | 进程、配置投影、Turn 执行 |
| Turn Service | Turn 准入、归因、执行状态、重生成操作、终态 | 进程、`opencode.json`、绝对路径推导 |
| History Projection | Runtime 消息与 Turn 归因合并、游标校验、相对路径转换 | 消息持久化、DB 降级历史 |
| Catalog | Agent/Model/Skill/MCP 定义、启停、凭据引用；向 Manager 提供投影输入快照 | Agent 级或 Workspace 级能力绑定 |
| OpenCode 适配层 | HTTP 协议、显式 `directory` 传参、session 生命周期、prompt/abort/revert、历史读取、健康与模型预检、事件归一化、投影内容与 launch spec 纯函数 | 进程、端口、文件写入、DB、Turn 领域规则、对客户端广播 |
| Sandbox | 进程或容器的启动/停止/状态/清理、进程组与 `/proc` 校验、stderr 日志、孤儿回收、端口分配、Runtime Home 目录与挂载、原子写文件、路径越界防护 | OpenCode 配置内容、session/turn、HTTP/SSE、发布决策 |

适配层可以按 Workspace 持有 HTTP 连接与事件映射状态，但不得成为第二份运行时事实源；健康、活动 Turn、发布状态只在 Manager 内存中。

### 3. 单 Server、单 SSE 与事件路由

- Manager 以 `workspace_id` 为键维护至多一个 Server。进程状态为 `stopped → starting → healthy → stopping → stopped`；发布状态为 `healthy → publishing → draining → restarting → healthy`；失败进入 `unavailable` 并记录原因。
- 每个 healthy Server 恰好一条 SSE 消费任务，由 Manager 在健康检查通过后建立、进程停止时取消。Turn Service、History、Channel、Scheduler 不得自行订阅 OpenCode。
- prompt 只等待 HTTP 接受结果，事件异步到达。Port 不提供 per-turn 事件流，也不以 turn 为订阅单位。
- `RuntimeEventEnvelope` 携带 `workspace_id`、`runtime_session_id`、`event_id`、`type`、`durable_seq`、`occurred_at`、`payload` 和 Runtime message id。字段以 C04 DTO 为准；本 ADR 只固定“身份字段必须可由 Runtime 证明”。
- `turn_id` 由 Manager 的活动执行登记与消息映射解析，adapter 不理解 Witty Turn。重复事件按 `(runtime_session_id, durable_seq)` 幂等；`event_id` 只用于诊断，不依赖其跨重连稳定。
- 进程重启后向消费者发 `runtime_restarted` / `resync_required`，要求全量重读，不重放 Delta。
- 每步 usage 由 adapter 原样映射为事件；聚合策略归展示层，不在 adapter 内按轮累计。

### 4. 三类路径与 Runtime Home

- `host_directory`：DB 中的宿主目录，创建时规范化并解析符号链接，P0 不允许改写。
- `runtime_directory`：OpenCode 实际看到的目录。`local_process` 等于 `host_directory`；`docker` 为容器内固定挂载点，由 Manager 依 Workspace 派生，客户端不可传入。
- `relative_path`：只用于前端预览、下载和文件 API。adapter 可以返回 Runtime 绝对路径，但必须在 seam 内转为 `workspace_id + relative_path`；拒绝 `..` 与符号链接逃逸。
- Runtime Home 独立于用户目录：`<WITTY_WORKSPACE_ROOT>/runtime/<workspace_id>/{config,data,state,cache}`。`XDG_CONFIG_HOME` 指向 `config`，`opencode.json` 位于 `config/opencode/opencode.json`。不再使用 `opencode-instances/<agent_id>` 或 `agent-workspaces/<agent_id>`。
- 路径推导唯一来源计划为 `witty_service/runtime/paths.py`（R03）。`workspace_paths.py` 随之删除，`storage/workspace_store.py` 与 `workspace_init.py` 重写；目录创建、挂载和权限由 sandbox 执行；DB 只保存 `host_directory`。

### 5. 进程、端口、句柄与清理

- 只有 sandbox 持有 `Popen`/容器句柄、pid、pgid 和 `process_start_time`；Manager 只持有不透明 handle 与 endpoint，不得 `os.kill` 或直接探测 `/proc`。
- 端口只存在于内存：sandbox 分配、探测复用并写回 handle；重启可换端口，adapter 每次从 handle 取 base_url。
- 停止顺序：adapter 尽力 `POST /instance/dispose` → sandbox 终止进程组 → `/proc` 校验收敛。未能终止必须显式报错，不谎报 `stopped`。
- 清理目标一律以 `workspace_id` 为键。杀掉 Workspace A 的进程或容器不得影响 B，不保留 Agent 级句柄。
- 空闲停止、重启和故障发现是 Manager 策略，执行归 sandbox。控制面重启采用 fail-stop 对账，不接管遗留进程（T09）。
- `_process_utils.py` 从 agent-server 迁入 `witty_service/sandbox/process_utils.py`；agent-server 不再 spawn 任何进程。`Dockerfile` 的 `CMD`、`HEALTHCHECK`（当前指向 `/ping`）与 COPY 内容同步改为 `opencode serve` 所需依赖，归 R02/R10 验证。

### 6. 投影、发布与 Turn Permit

- Manager 生成确定性投影：全部未归档 Agent、全部 enabled Model/Skill/MCP，不含 Session 激活与凭据本体。
- adapter 只提供纯函数：把投影输入序列化为 `opencode.json` 内容，并给出 launch spec（命令、环境变量、XDG 目录）。adapter 不写文件。
- 发布由 Manager 编排：冻结非密输入 → 生成内容 → 交 sandbox 原子写入 Runtime Home → 停止 → 启动 → adapter 健康与可见性检查 → 短事务更新 `published_projection_hash`。sandbox 是 Runtime Home 的唯一落盘方。
- Turn Permit 的“确认最新投影已发布 + 登记活动 Turn”只在 Manager 内存完成，不携带配置代次。adapter 与 sandbox 不感知 Permit。
- Turn Service 只获取和释放 Permit；不生成配置、不写 `opencode.json`、不调用 sandbox。
- 常规发布不杀在飞 Turn；10 秒是触发请求的等待上限；失败不以旧投影执行新 Turn。细节以上游方案 §4.2、§5.2 为准。

### 7. 保留、改造与删除清单

| 对象 | 处理 | 目标 |
| --- | --- | --- |
| `witty_agent_server/infra/clients/opencode_client.py`、`infra/clients/base.py` | 改造 | 只封装协议：所有请求显式带 `directory`；以 `runtime_session_id` 寻址；提供单条 `GET /event` 订阅；删除 `session_key`/标题查找和 per-turn `stream_turn`；`ClientBase` 可并入或被替换 |
| `witty_agent_server/runtimes/runtime_base.py`、`opencode_runtime.py` | 改造 | 保留事件映射；输出 `RuntimeEventEnvelope`；删除 per-turn 订阅、WS 终态和按轮 usage 聚合 |
| `witty_agent_server/application/services/agent/opencode_lifecycle_service.py` | 拆分 | 保留 launch spec、健康探测、provider/model 预检；`Popen`/终止与 XDG 目录创建迁入 sandbox；删除 `profile=agent_id` 命名与 `workspace_paths` 依赖 |
| `witty_agent_server/application/services/agent/_process_utils.py` | 迁移 | → `witty_service/sandbox/process_utils.py` |
| `witty_agent_server/api/**`、`app.py`、`main.py`、`logger/**` | 删除 | 旧 HTTP 入口、per-session WS 与独立日志配置 |
| `witty_agent_server/application/models/**`、`application/runtime_factory.py`、`adapters/runtime_registry.py` | 删除 | 单 Runtime 装配归组合根，Wire 模型归控制面 DTO |
| `witty_agent_server/application/services/agent/{base,errors,opencode_agent_service,runtime_workspace_resolver}.py`、`services/session/**`、`services/skill/**`、`services/{event_emitter,session_state_sync_service,session_ws_orchestrator,task_pool}.py`、`infra/persistence/**` | 删除 | Session/Turn 归控制面，Skill/MCP 物化归投影，事件广播归 Manager |
| `witty_service/adapter/**` | 删除（R04 后） | 不再存在 agent-server HTTP/WS 边界 |
| `witty_service/runtime/{stream_errors,usage,artifact_detector,opencode_mcp_config}.py`、`runtime/errors.py` | 保留 | 控制面拥有的共享代码，adapter 可导入；C04 删除 `RuntimeLeaseConflictError` 与 `runtime/__init__.py` 中的 `RuntimeLease` 导出 |
| `witty_service/sandbox/**` | 改造 | owner 由 Agent 改为 Workspace；handle 字段随 `workspace_id`；命令改为 `opencode serve`；删除 agent-server cmdline 识别与按 Agent 清理 |
| `witty_service/storage/workspace_store.py`、`workspace_init.py` | 重写 | Workspace 与 Runtime Home 布局 |
| `witty_service/workspace_paths.py` | 删除 | 由 `witty_service/runtime/paths.py` 取代 |
| `witty_service/application/runtime_config.py` | 删除 | 不再有 per-agent `/agent/start` payload 与 `profile=agent_key`，由投影替代 |
| `witty_service/application/agent_manager.py` | 拆分后删除 | 运行时编排归 Manager，回合归 Turn Service，定义归 Catalog；不得作为新主链 |
| `witty_service/runtime/contracts.py` | C04 重写 | 删除 `RuntimeLease` 与 `config_generation`；新增内存 `TurnPermit` 和执行/事件/历史/健康 DTO |
| `agent_runtime_state`、`agent_locks`、`runtime_type`、`runtime_session_key`、`remote_runtime_agent_id` | 删除 | 内存运行时状态、单 OpenCode 与 `runtime_session_id`（D07） |

### 8. M1 最薄链路

M1 只验证一条真实垂直切片，Fake 与真实 adapter 跑同一套 seam 契约测试：

1. 组合根装配 Manager，注入 Fake 或真实 adapter，再注入 sandbox backend；两者实现同一 `WorkspaceRuntimePort`。
2. 测试 Workspace 使用临时 `host_directory`，投影输入只含一个模型和一个 Primary Agent，不含 Skill/MCP。
3. R05 只验证投影内容确定性；测试用固定产物让 sandbox 启动一个 `opencode serve`，端口由 sandbox 分配，adapter 健康检查通过。
4. 通过 Manager 创建 Runtime Session，取得 `runtime_session_id`；M1 只在测试内持有，不写业务表。
5. 提交 prompt，显式携带稳定 Agent 内部名、provider/model 与文本/文件 Parts；`runtime_directory` 只在 ensure session 时固定，prompt 不含 tools map。首次提交必须省略 `resume`（或对同一 message_id 显式传 `true` 恢复执行）；断言 HTTP 接受，且 `resume:false` 的 admission 只表示已接受、不代表执行开始。
6. Manager 的单条 SSE 收到事件并映射为 envelope，断言消息增量、完成和 idle 可到达；M1 不写 Turn 终态。
7. 读取历史，返回与 provider 输出一致的 assistant 消息。
8. 启动第二个 Workspace，验证目录、进程、端口、健康互不影响；停止并重启第一个 Workspace 后历史仍可读。
9. 空库只含新表，链路中不存在 `witty_agent_server` HTTP 进程。

M1 不包含：Turn Permit 与 10 秒等待、投影发布与 hash、Session/Turn 业务表写入、历史投影合并、abort/revert/重生成/删除/控制面恢复。它们分别属于 M2 和 M4。

### 9. 边界检查

- 结构扫描：`src/witty_service` 中 `from witty_agent_server` / `import witty_agent_server` 只允许出现在 `witty_service/bootstrap.py`；`witty_service/sandbox` 不得引用 runtime 契约、Manager 或 adapter。
- C01 只记录规则；C07 把该扫描、Fake/真实 seam 同测和迁移单头固化为 CI 门禁。
- 任何新增跨层调用或例外，先修改本 ADR 并同步 Fake、真实实现和契约测试，不得在代码里临时绕过。

## Consequences

- Session/Turn 可以基于 Fake Runtime 独立开发，但 Fake 只能证明 Draft 可消费，不能证明真实 OpenCode 行为。
- 去掉 agent-server HTTP 与 per-session WS 后，链路少一跳，进程和事件订阅数量都按 Workspace 收敛；代价是适配层与控制面同进程，adapter 异常会直接影响控制面，需靠隔离与恢复流程兜底。
- 共享代码必须归属控制面，不能为了复用反向 import agent-server。
- 首版仍需完成现有 Agent 级生命周期代码的收窄；本 ADR 不授权保留新旧两套执行路径。
- Port 每次 Verified Frozen 后变更必须同步 Fake、真实实现和契约测试。
- tools 的 Session 过滤能力必须由真实探针证明；若不成立，P0 明确退为全局 MCP，而不是在 seam 上伪造隔离。

## Related

- [ADR 0001：按活动 Workspace 隔离 OpenCode Server](./0001-workspace-scoped-opencode-runtime.md)
- [目标控制面架构](../agent-session-workspace-evolution-plan.md#4-目标控制面架构)
- [执行任务清单](../agent-session-workspace-task-plan.md)
- [领域术语](../CONTEXT.md)
