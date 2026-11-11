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

## R06 Turn Permit 与唯一后台发布

- 发布编排放在 Manager 内，投影渲染是注入的纯函数（`OpenCodeProjectionRenderer`），渲染结果的相对路径以 `XDG_CONFIG_HOME` 为根；凭据由 `RuntimeCredentialResolver` 在发布开始时解析，只写进 `opencode.json`，不进入投影与 hash。
- Runtime Home 的写入统一走 `SandboxBackend.write_runtime_files`：逐文件临时写 + `os.replace` 原子替换，拒绝绝对路径、`..` 与逃逸 Runtime Home 的目标；Docker backend 暂不实现，发布失败即 503，留待 R10 对等改造。
- `acquire_turn_permit` 先在 `permit_lock` 内确认“最新投影 hash 已发布”，再登记活动 Turn；发布任务在后台单飞，后到请求 join 同一 task。等待上限 10s 只约束请求本身，超时不取消后台任务、不插入 running Turn。
- 发布期间新 Permit 被暂停（join 发布 task），发布先等 `drained` 事件排空活动 Turn 再写配置与重启；发布成功后重新读取投影，若期间又变更则当前请求 503，由下一 Turn 再触发，不自动连续重启。
- 配置隔离检查不读宿主 `$HOME` 哨兵，改为强约束 `XDG_CONFIG_HOME == HOME/config` 且 HOME 指向 Runtime Home，配合 R03 的环境白名单与 `replace_env=True`；哨兵断言仍由 `test_runtime_home_isolation.py` 覆盖。
- location 预热会创建真实 warm-up session 并提交一轮真实 prompt，等待 durable 历史中出现 `session.next.step.started`；OpenCode 1.17.13 要求 message id 以 `msg_` 开头，否则 prompt 直接 400。warm-up 会话当前无法删除（T08 才提供 delete），会残留在 Runtime data 中且产生一次真实模型调用。
- 预热只执行投影中第一个 agent/model 的回合，其余 agent、model、skill 仅做 location 可见性核对，避免每次发布对所有组合发起真实调用。
- 已发布 hash 的读取与保存由 `WorkspaceProjectionStore` 落在 Workspace 聚合（`published_projection_hash`），Manager 不 import persistence，也不新增配置版本表/`publish_id`/`config_generation`。
- R05 投影只携带 Skill 名称与内容摘要，不携带内容，因此 R06 无法物化 Skill 文件；发布仍把 Skill 纳入 location 可见性校验，未物化时发布失败（503）而不是静默跳过。Skill 落盘属于 E05（M3），R10 依赖 E05；在 E05 合入前，启用 Skill 的 Workspace 无法发布 Turn 属于已知边界。

## R07 单条事件链与 durable 重放

- 每个 healthy Workspace 只有一条 `/api/event` 消费链，由 `_start_locked` 创建、`_cleanup_handle` 取消；不存在 per-session 常驻第二 SSE，多 Session 事件复用同一条链并按 `runtime_session_id` 归因。
- 事件链每轮连接都先按 `after=seq` 分页重放（`hasMore` 驱动），再消费实时流；重放与实时重叠由 durable seq 去重吸收，因此不丢不重。重放集合是所有已见 Session 的游标 ∪ 当前活动 Turn 的 Session，保证“断连期间才产生首个 durable 事件”的 Turn 也能补回。
- 单个 Session 的 history 重放失败只跳过该 Session 并保留游标，不再把异常放大为整条链重建；`RuntimeSessionNotFoundError` 直接淘汰该游标。链重建只在订阅流本身失败时发生。
- durable seq 在事件投递完成后才推进，避免投递被取消时游标超前；`session_seq` 按进程内见过的 Session 保留（一个整数/会话），进程重启后依赖 v2 history 的持久 seq 继续，真实探针已验证跨进程 `after` 游标一致。
- delta 是 ephemeral：不重放、不按 seq 去重、只实时转发；队列满时直接丢弃。非 delta 事件缺少 `durable_seq` 一律丢弃，仅 `session.idle`/`session.error` 允许无 seq。
- Turn 归因取该 Session 最新注册的 Turn；Turn 一旦收到终态事件，后续到达的 Turn 级事件（含晚到 delta）不再投递，但 `session.idle`/`session.usage`/`session.error` 仍放行，避免吞掉会话复位信号。
- 重同步通知 `RuntimeResyncRequired` 覆盖四种原因：`runtime_restarted`（链因重启重建）、`event_stream_interrupted`（断流后每轮失败只发一次，避免 resync 风暴）、`subscriber_lagged`（慢消费被踢，先发 resync 再关流）、`runtime_stopped`（shutdown 主动关闭）。消费方收到后全量重读；`last_durable_seq` 是该 Workspace 内 Session 游标的最大值，仅作提示。
- 背压：内部 pump 队列上限 256，订阅者队列上限 64。delta 满则丢；非 delta 最多等 5s，超时即发 `subscriber_lagged` 并关闭该订阅，保证慢消费不阻塞其它订阅者与健康探测，终态通过重同步兜底而不是静默丢失。
- 链重建的固定重连间隔为 0.5s，且运行时非 healthy 时不再重试，等下一次显式启动重建，避免 runtime 已退出时形成重连风暴。
- 已知边界：`session_seq` 不按空闲 Session 淘汰（每个会话仅一个整数，重连时会对每个已见 Session 发一次 history）；活动 Turn 释放后其 Session 的断连重放仍会覆盖，但已无 UI 消费方，最终以 `event_stream_interrupted` 重同步为准。
