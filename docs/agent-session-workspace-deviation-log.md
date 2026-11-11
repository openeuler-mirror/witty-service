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

## R04 v2 client/adapter 垂直链路

- `prompt` 的 200 只表示 admission 被接受：`TurnAdmission` 只携带 `admittedSeq/delivery`，执行开始仍以事件流里的 `step.started` 为准；真实探针 `tests/probes/test_opencode_v2_seam.py` 按此断言。
- agent/model 在 admission 前用 location catalog 校验，并按会话当前值按需切换；无效 model 在 v2 会静默停住，不能等 OpenCode 报错。
- 真实 `/api/event` 的 `session.next.text.delta` 是 ephemeral（`durable: null`），durable 历史不保留 delta。因此 `RuntimeEventEnvelope.durable_seq` 改为 `int | None`：delta 为 `None`，不参与重放与按 seq 去重；逐字增量只能来自 `subscribe`，`replay_events` 只返回 durable 事件。
- 非 delta 事件若缺少 `durable.seq` 直接丢弃，避免产生不可重放、不可去重的“伪 durable”事件；`session.idle`/`session.error` 属于通知类事件，允许没有 seq，按实时事件转发但不参与重放。
- 1.17.13 的 `text.ended` 不携带 `finish`（`finish` 只在 `step.ended`），因此实时 `message.completed` 的 `finish` 恒为 `None`；回合成败以 `step.ended`/`step.failed` 表达，历史消息的 `finish` 仍取自 message 投影。
- `/history` 与 `/message` 的 `limit` 上限为 100，client 统一 clamp；`history` 的 `after` 是 durable seq，非法值返回 400。
- 历史 cursor 由 v2 的不透明 token 包装成 `<session_id>:<token>`：解码时校验会话身份，避免跨会话游标互换；`cursor` 原样透传，不解析其内部结构。
- 历史投影只保留 `user`/`assistant`/`agent-switched`/`model-switched`，`system` 等消息直接过滤；assistant 只映射 text/reasoning part，tool/file part 留待产物链路接入时再补。
- 本阶段 prompt 只接受文本 part，遇到 file part 显式返回 400；文件输入在 C04/R05 的产物链路里另行设计。
- `interrupt_session`/`revert`/`delete_session`/`inspect_session` 仍返回 501，按任务计划留到 T08 扩展操作时实现，不在 R04 扩大范围。
