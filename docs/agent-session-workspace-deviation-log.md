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

## R03 Runtime Home 与三路径

- 新增 `witty_service/runtime/paths.py`：Runtime Home 固定为 `<WITTY_WORKSPACE_ROOT>/runtime/<workspace_id>/{config,data,state,cache}`，逐级 0700；与用户工作目录、legacy `opencode-instances` 完全分离。
- `environment()` 只透传 PATH/SHELL/语言/时区/临时目录/代理等基础变量，`OPENCODE_*` 与调用方私有变量一律丢弃；HOME 与四个 XDG 目录强制指向 Runtime Home。实测 1.17.13 在只改 XDG 时仍会加载 `$HOME/.config/opencode/opencode.json`，所以 HOME 必须一起改；探针 `tests/probes/test_runtime_home_isolation.py` 用真实 binary 对照验证。
- `SandboxBackend.start` 新增 `replace_env`：Manager 启动时传 `True`，本地进程不再继承宿主环境。否则宿主的 `OPENCODE_CONFIG` 会经 `os.environ` 合并回子进程，配置隔离失效；legacy agent_manager 不传该参数，语义不变。
- `paths.ensure()` 拒绝预置的 Runtime Home 符号链接（`runtime/`、workspace 目录及四个子目录），避免通过软链把配置写到 Runtime Home 之外。
- `bootstrap.build_local_opencode_spec` 是 Runtime Home 的唯一落盘入口：先 `ensure()` 再生成 env 与 `opencode serve --print-logs` 命令；host_directory 直接取 `workspace.runtime_directory`，避免两份目录输入不一致。Manager 新增 `stderr_log_path` 透传，serve 日志写进 Runtime Home，不落在 Workspace 里。
- 本地进程启动后 opencode 需要数秒才 healthy，adapter `connect` 改成有界轮询（默认 30s，仍以 `/api/health` 为唯一判据）；超时、取消或异常都会关闭 client，`start` 阶段被取消则先等待沙箱返回句柄再清理，避免遗留进程。
- 偏差：Runtime Home 目录预创建在组合根完成，R06 发布配置内容时仍由 sandbox 原子写入，届时再统一下沉；legacy `workspace_paths.py` 与 `opencode_lifecycle_service` 保留给旧链路，U09 再删；Docker 的 Runtime Home 挂载与容器内路径映射留到 R10，本阶段只保证 `SandboxBackend.start(env=..., replace_env=...)` 契约对两种 backend 一致。
- `resolve_workspace_path`/`workspace_relative_path` 已提供产物相对路径转换与 `..`、符号链接逃逸校验，实际调用点在 R04/R07。
