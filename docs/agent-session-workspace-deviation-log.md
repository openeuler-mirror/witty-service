# Agent Session Workspace 偏差日志

## R01 Workspace Runtime Manager 骨架与组合根

- 新增 `witty_service/bootstrap.py` 作为唯一 `witty_agent_server` 导入边界；Manager 只依赖 `WorkspaceRuntimePort` 与 `SandboxBackend`。
- 旧 agent-server 与 legacy client 暂不删除：新 seam 走独立的 v2 client/adapter，旧入口留待 U09 清理，避免 R04 破坏仍在运行的旧链路。
- `SandboxBackend.start` 增加 `command` / `process_marker` / `stderr_log_path` 覆盖，使 Manager 能启动 OpenCode server；句柄归属仍在 R02 改为 workspace。
- R01 adapter 只实现 connect/health/inspect_workspace/ensure_session，其余端口方法显式返回 501，等 R04 补齐 admission/event/replay/history。
- 运行中若 runtime spec 变化，Manager 拒绝静默复用并要求 restart；adapter 拒绝在已连接目录之外创建 session。
