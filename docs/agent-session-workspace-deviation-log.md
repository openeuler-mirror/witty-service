# Agent Session Workspace 偏差日志

## R01 Workspace Runtime Manager 骨架与组合根

- 新增 `witty_service/bootstrap.py` 作为唯一 `witty_agent_server` 导入边界；Manager 只依赖 `WorkspaceRuntimePort` 与 `SandboxBackend`。
- 旧 agent-server 与 legacy client 暂不删除：新 seam 走独立的 v2 client/adapter，旧入口留待 U09 清理，避免 R04 破坏仍在运行的旧链路。
- `SandboxBackend.start` 增加 `command` / `process_marker` / `stderr_log_path` 覆盖，使 Manager 能启动 OpenCode server；句柄归属仍在 R02 改为 workspace。
- R01 adapter 只实现 connect/health/inspect_workspace/ensure_session，其余端口方法显式返回 501，等 R04 补齐 admission/event/replay/history。
- 运行中若 runtime spec 变化，Manager 拒绝静默复用并要求 restart；adapter 拒绝在已连接目录之外创建 session。

## R02 sandbox 按 Workspace 隔离

- `SandboxHandle.agent_id`/`start(agent_id=...)` 统一改为 `workspace_id`，`find_handle_by_agent_id` 改为 `find_handle_by_workspace`，Docker 容器名与 state payload 同步改键。
- 迁移期旧 agent 目录即 workspace：agent_manager 调用点传 `workspace_id=agent.id`，D07 删除旧持久化后再清理。
- 孤儿扫描识别 `witty_agent_server.app:create_app` 与 `opencode serve`；同组子进程（opencode serve）通过已登记 leader 的 pid + start_time 豁免，避免启动回收误杀已登记 workspace。
- `find_handle_by_workspace` 优先返回自成进程组的 leader，避免只杀 opencode 子进程而留下 leader；无 leader 时才退回子进程句柄。
