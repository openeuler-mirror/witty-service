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

## R08 空闲停止与进程生命周期对账

- 生命周期对账只有一条链路：`reconcile()` 遍历所有已登记 Workspace，逐个 `sandbox.status` 判定“已退出 / 仍运行”，再决定标记不可用或空闲停止；同一函数既是后台 monitor 的周期动作，也是测试与运维手动触发的入口。monitor 由首次成功启动触发惰性创建，`shutdown` 先取消 monitor 再停执行体。
- 进程退出判定统一为 `stopped`/`error`（`_has_exited`），`starting` 不算退出，避免把仍在启动的执行体误标不可用；`health()` 与 `reconcile()` 共用同一判定，保证读路径与后台对账结论一致，不会出现“读模型可用但后台已关链”的分裂。
- 崩溃即标记不可用：停事件链、清空 base_url/sandbox_id、置 `unhealthy` 并写入 `sandbox is <status>`，随后回收 sandbox 句柄（`cleanup`），不自动重启、不自动 resume。回收失败只记日志，句柄留待下一次显式启动时重试清理。
- 空闲停止以 `last_activity` 为准，更新点覆盖启动成功、Permit 获取/释放、发布成功、以及事件投递；`active_turns` 非空、存在在飞发布任务、或发布状态不是 `idle`/`failed` 时一律不空闲。发布失败进入 `failed` 后仍允许空闲停止，避免一次发布失败让进程与端口永久常驻。
- 空闲停止在 `permit_lock` 内二次确认后置 `stopping`，再停链、断连、`sandbox.cleanup`；停止完成置 `stopped` 并向订阅者发 `runtime_stopped` 关闭流，因此重复对账无副作用、端口立即释放、下一次 `ensure_started` 复用 `preferred_port`。
- 停止失败置 `error` 并保留句柄，下一轮对账直接重试回收（幂等），不会因一次沙箱抖动永久泄漏进程与端口。
- 空闲超时默认 900s、对账间隔默认 30s，均为构造参数；配置化空闲策略属 I04，本阶段不引入设置项。

## R09 健康、故障隔离与可用性读模型

- `runtime_status` 读模型由 Manager 独占：`RuntimeStatus`（stopped/starting/ready/restarting/unavailable）是 `RuntimePhase` 与 `PublicationPhase` 的投影，`availability()` 同步、纯内存、不探测进程，可被纯 DB 列表逐条调用；明细错误只留在 `get_state().last_error`，对外 `detail` 是固定非密文案，避免绝对路径、命令行或上游错误文本外泄。
- 映射优先级：`unhealthy`/`error` → unavailable；发布在飞（`publish_task` 或 publishing/draining/restarting/verifying）→ restarting，即使此时进程正处于 stopping/starting 也对外统一显示 restarting；`starting` → starting；`healthy` → ready；其余（含显式停止中的 stopping）→ stopped。发布 `failed` 不算在飞，仍为 ready，历史可读。
- 崩溃发现依赖 R08 对账，`availability()` 不阻塞探测，因此在下次对账（默认 30s）前可能仍报 ready；这是纯内存读模型的已知滞后，读请求失败会由 Port 侧返回 503 兜底。
- `require_ready()` 是 Runtime 读入口的最后一道门禁：400/404/409 校验完成后调用，非 ready 抛 `RuntimeUnavailableError`（503 / `RUNTIME_UNAVAILABLE`），错误优先级沿用 `api.errors.ERROR_PRIORITY` 的 400→404→409→503，不新增错误码。
- 该门禁只适用于确已有 `runtime_session_id` 的历史与产物读取；未建立 Runtime Session 的会话应返回空历史，不因 Runtime 未启动被判 503（由 B 线在调用门禁前判定）。
- 单 Workspace 故障不扩散：`availability()` 只做单键内存读取、未知 Workspace 返回 stopped，不抛异常；故障 Workspace 的 unavailable 不影响其他 Workspace 的 ready 判定，纯 DB 列表可照常返回。

## T01～T04 Session 与 Turn 准入

- T01 只建立 Session 行（`runtime_session_id=NULL`），不启动 Runtime Server；列表/详情从 Manager 的 `availability()` 读取权威 `RuntimeStatus`，与 R09 保持单一来源，不再自行由 `RuntimePhase` 映射（发布在飞显示 `restarting`）。
- Session 列表游标绑定 Workspace 身份，Turn 列表游标绑定 Session 身份；非法或跨作用域游标统一 400，分页键使用 `(pinned,last_activity_at,id)` 与 `turn_seq`，不解析 v2 游标。
- T02 的选择更新只写 `current_agent_id/current_model_id`，仓库层按非空列部分更新；不触碰 `runtime_session_id`、历史 Turn 快照，也不启动 Runtime。
- T03 保留唯一准入路径 `SessionRepository.start_turn`：Session 当前选择在插入事务内重新读取并校验，调用方传值不一致时拒绝，避免 TOCTOU 归因；seq 分配与 running 插入同事务，冲突映射 409 `ACTIVE_TURN_CONFLICT`。
- T04 归因快照在准入事务内写入，历史只读 Turn 快照；未用快照重建执行环境。Turn 列表按 `turn_seq` 倒序分页。
- R06/R07 已合入本分支；T05 起经 Manager 的 `acquire_turn_permit/release_turn_permit/bind_turn_runtime_session`、单条事件链与历史门面接入，不以 Fake 放行。R09 的 `runtime_status` 与 503 `RUNTIME_UNAVAILABLE` 已在 T07 接入，并由 Session 契约测试与历史 503 契约测试校验。

## T05 Turn 执行管线

- A 线 R04/R06/R07 的 `WorkspaceRuntimeManager` 只暴露 Permit/事件订阅，未暴露 `ensure_session/admit_turn` 公共门面；T05 在 Manager 补齐 `ensure_runtime_session` 与 `admit_turn` 两个纯委托方法（不写 adapter/sandbox/projection/publication/事件链），否则 Turn Service 只能绕过 Manager 直连 Port。
- 固定顺序：短事务读取并校验 Session 当前选择 → `ensure_started` → `acquire_turn_permit`（发布与 location 就绪在此门禁，失败不插 running）→ 最终短事务 `start_turn` 复核并插 running → `ensure_runtime_session` 并持久化 Runtime Session ID → `bind_turn_runtime_session` → `admit_turn`。`admit_turn` 的 200 只表示 accepted，Turn 保持 running，不把 admitted 当 started。
- Permit 在失败/取消出口 `finally` 释放；成功准入后 Permit 由 Turn 事件泵在终态事件到达时释放，覆盖整个在飞回合，避免 R06 发布把仍在执行的 Turn 当成已排空。
- 每个 Workspace 一条内部事件泵（复用 Manager 单条事件链，不新增 SSE）；只处理 `turn.completed`/`turn.failed`/`session.error` 写终态并释放 Permit，其它事件留给 T06/T07。事件到达时若 Turn 已是终态，`finish_turn` 的条件更新不会复活它。
- 事件泵用 ready 事件保证队列注册完成后才准入；`runtime_restarted`/`runtime_exited`/`subscriber_lagged`/`runtime_stopped` 说明事件可能已丢或 Runtime 已退出，先把该 Workspace 仍持有 Permit 的 Turn 落 `interrupted` 并释放 Permit，不再假装仍在 running；`runtime_exited` 与 `runtime_stopped` 后直接结束事件泵，`runtime_restarted`/`subscriber_lagged` 仅在 Runtime healthy 时重订阅，避免对已停止的 Runtime 空转。
- admission 失败发生在 running 插入之后时，Turn 立即落 `error`（错误码取 DomainError，否则 `INTERNAL_ERROR`），再释放 Permit；Runtime Session 持久化失败同样落 `error` 且不写 Runtime Session ID。
- 文本 Parts 由 API DTO 转成 Runtime Parts；文件 Parts 在 T05 不支持，在拉起 Runtime 与插入 running 之前直接 400，避免留下 error Turn 与消耗 `turn_seq`。`message_id` 固定为 `msg_<turn_id>`，同一 Turn 重试保持稳定。
- Runtime Session 每回合都经 `ensure_runtime_session` 复核并覆盖为返回的 ID，兼容 Runtime 重启后旧 ID 失效；单 Session 唯一 running Turn 保证并发覆盖不会发生。
- 新增 `TurnService.close()`：先置 closing 标志让在途请求在下一个 await 后退出，再取消事件泵，把仍持有 Permit 的 running Turn 落 `interrupted` 并逐个隔离释放 Permit，最后关闭 Manager。

## T06 turn_runtime_messages 消息归属

- 用户消息在 `admit_turn` 返回成功后以固定 ID `msg_<turn_id>`、`user`、ordinal 0 写入；admission 失败不会留下没有 Runtime 对应物的消息映射。
- Assistant 消息只在 `message.started`/`message.completed` 边界事件映射，`message.delta` 不落库：Delta 只携带增量正文且高频，重复查询会拖慢事件泵；同一 `runtime_message_id` 的边界事件已足够建立唯一归属。
- 映射只记录 `runtime_message_id/turn_id/role/ordinal/created_at`，不保存正文、Parts、Delta 或 Tool 副本；幂等由 `add_runtime_message` 的 `on_conflict_do_nothing(runtime_message_id)` 加冲突一致性校验兜底，表级 `(turn_id, ordinal)` 唯一约束防止序号错乱。
- Assistant ordinal 取当前已映射消息的 `max(ordinal)+1` 且最小为 1，把 0 永久预留给用户消息；即使 Assistant 边界事件先于用户消息落库也不会占用 0 或与用户消息冲突。
- 只有 `TurnStatus.running` 的 Turn 接受消息映射；终态后晚到的 `message.started`/`message.completed` 直接丢弃，不复活 Turn、不新增归属。
- `agent-switched`/`model-switched` 只存在于 v2 历史消息，不会出现在 live 的 text 事件流；本阶段消息映射只消费带 `runtime_message_id` 的文本边界事件，控制消息过滤与映射留给 T07 History Projection。

## T07 v2 History Projection 与 Session 历史/事件读模型

- 新增 `HistoryService`：消息页只读 v2 Runtime 历史，DB 仅用于把 `runtime_message_id` 合并回 `turn_runtime_messages` 获取 Turn 归因；Session 没有 Runtime Session 时返回空页，不读取 DB 消息表。
- 消息接口改为 `limit/cursor` 入参、`next_cursor/previous_cursor` 出参；对外游标是带 Session 作用域的 base64 不透明串，内部原样携带 v2 的 `cursor.next/previous`，跨 Session 使用返回 400 `CURSOR_SESSION_MISMATCH`，非法游标返回 400 `INVALID_CURSOR`。
- `agent_switched/model_switched` 控制消息在投影层过滤，不伪装成 user/assistant；过滤只影响当前页条目数，v2 返回的 next/previous 游标原样保留，翻页边界不因过滤而漂移。
- Runtime 非 ready（含发布中 `restarting`、进程退出 `unavailable`）时返回 503 `RUNTIME_UNAVAILABLE`，不回退 DB 历史；Session 不存在或已删除返回 404。
- Session 事件流以 `data: <SessionEvent JSON>` 输出，不写 SSE `id:`/`event:`/`retry:`；Turn/消息事件按 Turn 所属 Session 过滤，session 级事件在订阅后 Runtime Session ID 才落库或重启后变化时会重新读取 Session，避免用订阅瞬间的快照漏事件；`RuntimeResyncRequired` 统一映射为 `resync_required`，payload 标记 `full_reload=true` 并携带 `reason/last_durable_seq`，要求客户端全量重读 Turn 与历史。
- Manager 增加 `read_history` 纯委托门面，并统一用 R09 的 `require_ready()` 作为最后门禁（400/404 判定后调用）；事件重连与 durable seq 去重仍由 Manager 单 Workspace 事件链负责，API 只消费其重放后的 `RuntimeStreamItem`。
