# ADR 0001: 按活动 Workspace 隔离 OpenCode Server

- 状态：Accepted
- 日期：2026-10-31

## Context

目标产品允许同一 Session 切换 Agent 与模型，并要求 Session 固定属于一个可复用 Workspace。Agent Definition 与全局 Skill/MCP catalog 可跨 Workspace 复用，但工作目录、目录指令、运行事件和 MCP 连接不能串用。

OpenCode 1.17.13 已实测能够在单个 Server 中通过 `directory` 参数服务多个工作目录，因此“每活动 Workspace 一个 Server”不是实现多 Workspace 的唯一方式。

当前实现把 Server、sandbox 和工作目录绑定在 Agent 上。若沿用该边界，切换 Agent 会意外切换工作目录和 Runtime Session，无法满足连续对话。

## Decision

以 Workspace 作为 OpenCode Runtime 的隔离边界：

- 每个活动 Workspace 至多运行一个 OpenCode Server。
- 同 Workspace 的多个 Session 共享该 Server，并可复用各自持久化的 Runtime Session。
- 每个 Workspace 使用独立 Runtime Home、目录挂载、配置发布流程、事件流和 MCP 连接。
- 所有 OpenCode 请求仍显式携带该 Workspace 的 `runtime_directory`，不把进程 cwd 当作隐式正确性的唯一保障。
- Server 按需启动、空闲停止；进程状态不是领域事实，不建立 Runtime Server 表。
- Agent 与模型是 Turn 级选择，不再是进程或 Server 的边界。
- 配置保存不立即暂停准入；下一 Turn 通过 Turn Permit 确保最新确定性投影已发布。发布暂停新 Permit，等待活动 Turn 自然结束，再原子写入、重启 Server 并通过健康检查；失败时禁止用旧配置执行新 Turn。
- 常规发布永不自动杀死在飞 Turn。触发请求的 10 秒等待上限不取消内部唯一后台发布，也不改变发布状态。
- 控制面进程重启采用 fail-stop 对账，不续跑或自动重放遗留执行。

## Consequences

### 正面

- Agent 切换不再导致目录、Runtime Session 或 MCP 连接切换。
- 配置刷新、事件路由和故障恢复只需面对一个明确的 Workspace 边界。
- 一个 Workspace 的进程或 MCP 故障不会直接影响其他 Workspace。
- 本地与 Docker 可以使用同一套 host/runtime/relative path 契约。
- 相比每 Session 一个 Server，进程与 MCP 连接数量更可控。

### 代价

- 相比单 Server 多 directory，活动 Workspace 较多时进程和连接开销更高。
- 一个 Workspace 的 Server 故障会影响该 Workspace 中的多个 Session。
- Workspace 内配置发布需要排空在飞 Turn，可能阻塞该 Workspace 的新执行。
- Runtime Manager 必须实现可靠的空闲回收、fail-stop 恢复和 Turn Permit/后台发布，而不能只启动进程。

这些代价首版被明确接受，以换取更简单、可验证的一致性边界。

## Alternatives Considered

### 单 Server 多 directory

优点是进程和连接开销较低，且原生 API 已证明可行。未采用的原因是 directory instance 仍共享配置、MCP 生命周期和事件处理，配置原子发布与故障隔离更难验证。若未来真实资源指标证明当前拓扑成本过高，可基于同一 Workspace 领域边界重新评估，而不改变 Session/Turn API。

### 每 Session 一个 Server

隔离看似更强，但会放大进程、事件流和 MCP 连接数量。Session 是对话上下文而非目录隔离单位，因此不采用。

### 延续每 Agent 一个 Server

与 Session 内 Agent 切换、Agent 跨 Workspace 复用和连续上下文直接冲突，予以移除。

## Related

- [Agent、Session 与 Workspace 演进方案](../agent-session-workspace-evolution-plan.md)
- [领域术语](../CONTEXT.md)
