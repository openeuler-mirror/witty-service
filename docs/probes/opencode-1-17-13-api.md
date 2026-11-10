# OpenCode 1.17.13 `/api`（v2）能力探针

| 项 | 值 |
| --- | --- |
| 定位 | v2 面的证据套件；legacy 套件保留为历史证据 |
| 目标版本 | `opencode-ai@1.17.13`（pinned，校验方式与 legacy 套件相同） |
| 套件 | `tests/probes/test_opencode_1_17_13_api.py`（29 个用例） |
| 范围 | 会话与 surface、事件作用域、interrupt/revert、会话级配置、跨会话隔离 |
| 不包含 | legacy 结论复用、并发事件量、revert 的 git diff、多进程部署（见 §7） |
| 结果 | 3 次连续全量运行通过：`144.18s` / `163.67s` / `147.43s`（2026-11-10） |

> 本文件只记录 v2 的实测行为与对 C04 的约束；不改 `opencode-1-17-13.md`。

## 1. 运行方式

```bash
WITTY_PROBE_OPENCODE_DIR=/opt/witty-service/opencode-1.17.13 \
  .venv/bin/python -m pytest tests/probes/test_opencode_1_17_13_api.py \
  -m probe -q
```

- 复用 `ProbeEnv` / `MockProvider` / `EventCollector`，独立 `HOME` 与 XDG、独立进程组。
- 默认 pytest 收集仍为 0（`addopts = -m "not probe"`），不会进入常规门禁。
- 单文件全量约 155s；跨进程重启用例使用独立的 `standalone_env`。

## 2. 用例表

| v2 用例 | 对应 legacy 用例 | 核心断言 |
| --- | --- | --- |
| `test_create_admission_and_turn_projection` | P01/P02/P20 | create 无 title；admission 返回 `id/admittedSeq/delivery`；同 id+同 text 幂等 200；同 id+不同 text 为 409；一轮事件序列固定为 admitted→prompted→step.started→text.started→text.ended→step.ended |
| `test_agent_and_model_switch` | P05/P06 | `/agent`、`/model` 返回 204，产生 switched 事件与 `agent-switched`/`model-switched` 消息；下一轮 provider 实际使用新 agent/model |
| `test_cross_face_projection_isolation` | 无 | v2 建会话在 legacy `message` 为空；legacy 建会话在 v2 `history`/`message` 为空；同一 id 两面 GET 都 200 |
| `test_error_status_matrix` | P22 | 非法 id 400、未知会话 404、history/message 游标 400、prompt body 400、revert 非法 message 404；`/wait` 未实现为 503 |
| `test_event_scope_and_delta` | P12/P13 | durable 无 delta；`/api/event` 全局且有 delta；legacy `/event?directory=` 能看到同一 v2 会话；无 directory/其他 directory 看不到；`active` 全局，legacy `/session/status` 看不到 v2 |
| `test_interrupt_keeps_partial_assistant` | P11 | 忙时 interrupt 204；部分文本保留；assistant `finish=error`、`error.message=Provider turn interrupted`；durable 出现 `step.failed` |
| `test_revert_stage_does_not_trim_context` | P09 | stage 返回 `messageID/diff/files` 并写 `session.revert`；只追加 `revert.staged` 事件，不改消息投影；下一轮仍带全部历史 |
| `test_revert_commit_truncates_after_staged_user` | P10 | commit 保留被 stage 的 user，删除其 assistant 以及之后的消息；下一轮上下文按裁剪后的投影；写 `revert.committed` |
| `test_revert_clear_keeps_projection` | P10 | clear 只清 `session.revert` 并写 `revert.cleared`，投影不变；忙时 stage/commit 仍可用（不返回 409） |
| `test_p22_v2_history_and_message_pagination` | P22 | history 升序、`seq` 连续、`limit≤100`、`after` 生效、`hasMore` 正确；message 降序、`cursor.next/previous` 双向分页且不重叠、`limit=101` 仍可用 |
| `test_v2_resume_false_only_admits_then_resume_true_executes` | P20 重写 | `resume:false` 只写 `prompt.admitted`，provider 0 次；`resume:true` 同 id 执行一次；重复 `resume:true` 幂等 200（带 `promotedSeq`）且 provider 不重复 |
| `test_v2_busy_prompt_admission_before_previous_step_end` | 无 | 忙时二次 prompt 的 `admitted` 在上一轮 `step.ended` 之前，`prompted` 在其之后；provider 第二次请求包含两条 user 文本（steer） |
| `test_v2_invalid_model_and_agent_at_create` | 无 | 非法 model 在 create 被接受，prompt 仍 200 但回合静默停在 `prompted`；非法 agent 在 create 被接受，回合照常执行但 `session.agent` 保留脏值 |
| `test_v2_location_readiness_requires_retry_not_global_catalog` | 无 | 新 location 的服务异步启动；全局 `/api/model` 就绪不代表可执行；必须预热/重试后才能稳定跑回合 |
| `test_v2_restart_keeps_history_and_after_cursor` | P14/P15 重写 | 跨进程重启后 history/message 完全一致；`after=seq` 仍可用；预热后原会话可继续，上下文包含此前全部 user 文本 |
| `test_p01_version_and_package_identity` | P01 | `/global/health` 是唯一版本来源；`/api/health` 只有 `{healthy:true}`；`/api/location` 默认 directory=server cwd、`project.id=global`；`/api/provider`、`/api/command`、`/api/fs/list`、`/api/pty` 均 200 JSON |
| `test_p02_session_agent_switch` | P02 重写 | v2 无逐轮 agent；`/agent` 会话级切换 A→B→build，每轮 provider system 与 `session.agent` 同步，产生 3 条 `agent.switched` |
| `test_p03_session_model_switch` | P03 重写 | `/model` 会话级切换后粘性；切回默认生效；新会话不继承；create 显式 model 生效 |
| `test_p05_v2_default_toolset_and_sanitize` | P05 重写 | v2 默认工具集含 `apply_patch`/`websearch`，**没有 `task`、没有 MCP 工具**，也无 MCP 原始名 |
| `test_p06_prompt_tools_field_ignored` | P06 重写 | prompt 携带 `tools` 被静默忽略（200，工具集不变）；后续轮仍是全量工具，无粘性状态 |
| `test_p07_mcp_tools_absent_from_v2` | P07 重写 | MCP 连接正常且 legacy 可见，但 v2 工具集不含 MCP；`/api/mcp`、`/api/config` 不存在（SPA HTML）；调用返回 `Unknown tool` |
| `test_p08_skill_exposure` | P08 重写 | `/api/skill` 返回 `probe-skill`；`skill` 在 baseline；调用结果含 `SKILL_BODY` |
| `test_p14_restart_boundary` | P14 重写 | create 时指定的 model/agent 跨进程重启保留；重启后原会话继续用 B + A，上下文累积 |
| `test_p15_restart_id_stability` | P15 重写 | 重启后 message id 与 part id 完全一致；新轮 id 与旧 id 不冲突 |
| `test_p16_tools_boundaries_ignored` | P16 重写 | 空/未知 key/原始 MCP 名/禁用内建/恢复 MCP 五组 `tools` 全部被忽略；create 的 `tools` 被丢弃 |
| `test_p17_default_location` | P17 重写 | 省略 location 时使用 server cwd；`bash pwd` 返回该目录；不存在的 directory 在 create 时仍返回 200 |
| `test_p18_invalid_switch_recovery` | P18 重写 | 非法 model switch 204→回合静默停住，切回合法 model 可恢复；非法 agent switch 204→按默认 agent 执行且保留脏值 |
| `test_p19_provider_failure_is_bounded` | P19 重写 | ERROR500 精确 3 次请求后 `step.failed`；TIMEOUT 精确 1 次即 failed；无 `session.error`，active 归零 |
| `test_p21_cross_session_isolation` | P21 重写 | A 的 agent/model 切换不影响 B；B 的事件里没有 switched；再从 B 切 agent 反向验证 |

## 3. 关键实测结论

### 3.1 两面关系

- session id 命名空间共享：同一 id 在 legacy 与 v2 的 GET 都返回 200。
- **消息/历史投影隔离**：v2 建的会话在 legacy `message` 为空；legacy 建的会话在 v2
  `history`/`message` 为空。C04 选定 v2 后，不能从 legacy 面补读历史。
- **事件总线共享**：legacy `/event?directory=<dir>` 能看到同一 directory 下 v2 会话的
  `session.next.*` 事件；但 payload 形状不同（见 3.2）。

### 3.2 流式与事件形状

| 流 | delta | 作用域 | payload | 游标 |
| --- | --- | --- | --- | --- |
| `/api/session/{id}/event` | **无** | 单会话 | `data`，无 `location` | 事件内 `durable.seq`；无 SSE id |
| `/api/event` | **有** | 全局 | `data` + 顶层 `location.directory` | `durable.seq` |
| legacy `/event?directory=` | 有 | directory | `properties`（无 `data`/`durable`） | 无 |

- `text.delta` 的 `data` 形状：`{timestamp, sessionID, assistantMessageID, textID, delta}`。
- 终态：`session.next.step.ended`（成功）、`step.failed`（中断/失败）、`session.error`、
  `session.idle`。`step.ended.data` 含 `assistantMessageID/finish/cost/tokens`。
- **逐字 UI 只能订阅 `/api/event` 并按 `sessionID` 过滤**；durable 流用于重放与终态。
- `history?after=seq` 与 `/api/event?after=seq` 都能完整重放；**跨进程重启后仍然成立**。

### 3.3 admission 与幂等

- `POST /api/session/{id}/prompt` 返回：
  `{id, admittedSeq, sessionID, prompt:{text}, delivery, timeCreated}`；
  执行完成后重放会多一个 `promotedSeq`。
- **同一 id + 相同 text**：200 幂等，`admittedSeq` 不变，provider 不重复执行。
- **同一 id + 不同 text**，或同 id 但 `resume` 标志冲突：409 `ConflictError`。
  C04 的幂等键必须同时锁定 `(id, text, resume)`，不能只比 id。
- `resume:false` 只 admit，不执行；`resume:true` 用同一 id 恢复执行一次。
- `delivery` 默认就是 `"steer"`，不能用来判断“会话是否忙”。

### 3.4 忙时二次 prompt

- 第二次 `prompt.admitted` 会在上一轮 `step.ended` **之前**出现；
  `session.next.prompted` 在上一轮结束之后才出现。
- 第二次内容以 steer 方式追加到同一执行体，provider 的第二次请求能看到两条 user 文本。
- 事件顺序不能按“一轮完整结束后才处理下一条 admission”来假设。

### 3.5 location 与 model 就绪

- `POST /api/session` 的 `location.directory` 在 create 时固定；prompt 不接受 directory。
- create **没有 title 入参**，传 `title` 被忽略；标题自动生成为 `New session - <ISO>`。
  因此按 title 重解析会话的做法在 v2 不成立，C04 必须持久化 session id。
- location 服务与 model catalog **按 location 异步启动**。未就绪时 prompt 仍返回 200，
  但回合会静默停在 `prompted`：没有 `step.started`、没有 `session.error`、provider 0 次。
- 全局 `/api/model` 是否包含目标 model 不能作为该 location 可执行的判据；套件用
  “独立 session 反复投递直到出现 step.started”来预热 location。
- **非法 model 同样静默停住**（create 接受、prompt 200、无错误事件）；非法 agent 则
  照常执行，但 `session.agent` 保留非法值，消息会带着脏 agent。
  C04 必须在 admission 前自行校验 model/agent，不能等 OpenCode 报错。
- create 对不存在的 directory 也返回 200（不做校验）；工作区有效性由调用方保证。
  该行为由 `test_p17_default_location` 断言。

### 3.6 interrupt

- 忙时与空闲时 `POST /interrupt` 都返回 204；未知会话 404 `SessionNotFoundError`。
- 中断后已生成的部分 assistant 文本保留；message 投影中 `finish="error"`，
  `error.message="Provider turn interrupted"`。
- durable/native 事件出现 `session.next.step.failed`，`data.assistantMessageID` 指向该 assistant。
- durable 流仍然没有 delta。

### 3.7 revert（与 legacy 语义不同）

- `POST /revert/stage {messageID}`：200，返回 `{messageID, diff, files}`；写
  `session.revert` 与 `session.next.revert.staged` 事件；**不改消息投影，也不裁剪上下文**。
- `POST /revert/commit`：204，清 `session.revert`，写 `revert.committed`；
  **保留被 stage 的 user，删除其 assistant 及之后的所有消息**，下一轮上下文按裁剪后的投影。
- `POST /revert/clear`：204，清 `session.revert`，写 `revert.cleared`；投影不变。
- commit/clear 在没有 stage 时也返回 204；stage 缺失 `messageID` 400，未知 message 404。
- 忙时 stage/commit 仍返回 200/204，不返回 409。
- 因此 legacy P09/P10 的“stage 后下一轮自动裁剪”结论**不能平移到 v2**：
  v2 必须显式 commit 才裁剪。

### 3.8 其他端点与分页

- `POST /api/session/{id}/wait` 未实现：503 `ServiceUnavailableError`。C04 不能依赖 wait。
- `GET /api/session/active` 是全局的，值为 `{sessionID: {type: "running"}}`；空闲为 `{}`。
- legacy `/session/status` 完全看不到 v2 会话（带不带 directory 都不行）。
- history：升序，`limit` 1–100（101/0 → 400），`after` 为非负整数（非法 → 400），
  `hasMore` 表示还有更晚的事件。
- message：**降序**（最新在前），`limit≥1`（0 → 400，101 可用，由
  `test_p22_v2_history_and_message_pagination` 断言），非法 `cursor` → 400；
  `cursor.next` 翻更旧、`cursor.previous` 翻更新。`before`/`after` 不是 message 的游标参数。
- history 事件形状：`{id: evt_, type, durable:{aggregateID=sessionID, seq, version}, data:{sessionID,...}}`。
- message 投影除 user/assistant 外，还会出现 `agent-switched`、`model-switched` 控制消息；
  assistant 的 `model` 是 `{id, providerID, variant}`。

### 3.9 v2 只读接口的覆盖面

- v2 存在：`/api/health`、`/api/location`、`/api/agent`、`/api/skill`、`/api/model`、
  `/api/provider`、`/api/command`、`/api/event`、`/api/session/active`、`/api/fs/*`、`/api/pty`。
- **不存在 `/api/mcp` 与 `/api/config`**：请求会命中 SPA 兜底，返回 200 + `text/html`，
  不是 JSON 错误。C04 的客户端必须先校验 `content-type`，否则会把 HTML 当数据。
- `/api/health` 只返回 `{healthy:true}`，不带版本；版本仍只能从 `/global/health` 取。
- `/api/location` 返回扁平 `{directory, project}`（无 `data` 包装）；`/api/agent`、`/api/skill`、
  `/api/model` 返回 `{location, data:[...]}`，location 用 deepObject 查询参数
  `location[directory]=...` 指定。
- location 未预热时 `/api/agent`、`/api/skill` 的 `data` 为空数组，不能据此判断“没有 agent/skill”。
- 上述 `/api/provider`、`/api/command`、`/api/fs/list`、`/api/pty` 的存在性由
  `test_p01_version_and_package_identity` 断言；`/api/mcp` 与 `/api/config` 的 SPA 兜底
  由 `test_p07_mcp_tools_absent_from_v2` 断言。

### 3.10 v2 没有逐轮配置

- create 只接受 `{id, agent, model, location}`；prompt 只接受
  `{id, prompt:{text, files?, agents?}, delivery, resume}`。
- **没有逐轮 agent/model/tools/directory**。传未知字段（如 `tools`）不会报错，会被静默丢弃：
  prompt 携带 `tools:{bash:false}` 仍返回 200，provider 依旧拿到全部工具。
- agent/model 只能通过 `POST /api/session/{id}/agent|model` 做**会话级**切换，切换后对后续轮粘性生效。
- 结论：C04 不能按“每次请求带工具开关/模型覆盖”设计入口；工具裁剪只能走配置级手段。

### 3.11 v2 工具集与 legacy 的差异

- v2 默认工具集为 12 个：`apply_patch`、`bash`、`edit`、`glob`、`grep`、`question`、
  `read`、`skill`、`todowrite`、`webfetch`、`websearch`、`write`。
- 与 legacy 的可见差异：**没有 `task`**；**MCP 工具完全不在集合里**（legacy 在同一
  `ProbeEnv` 下可见 `probe_echo_*`）。
- MCP 连接本身是正常的（legacy `/mcp` 显示 `connected`），因此不是环境问题。
- 在 v2 回合里调用 `probe_echo_upper` 会得到 `Unknown tool: probe_echo_upper`，
  不会触达 MCP server。
- Skill 不受影响：`skill` 在工具集中，`/api/skill` 能列出 `probe-skill`，调用结果包含技能正文。

### 3.12 非法 switch 与恢复

- 非法 model：`POST /model` 返回 204 并写入 `session.model`；prompt 仍 200，但回合静默停在
  `prompted`（provider 0 次）。切回合法 model 后无需重建会话即可恢复执行。
- 非法 agent：`POST /agent` 返回 204 并保留脏值；回合按默认 agent 继续执行，
  provider system 既不是 probe-a 也不是 probe-b。
- 与 create 时的行为一致：v2 不校验、不报错，C04 必须在调用前自行校验。

### 3.13 provider 失败是有界的

- ERROR500：共 3 次 provider 请求后落 `session.next.step.failed`，
  `data.error.message` 含 `HTTP 500`；assistant `finish="error"`、`content=[]`。
- TIMEOUT（transport 失败）：1 次请求即落 `step.failed`，错误信息含 `transport`。
- 两种失败都不产生 `session.error`，durable 里也**没有** `retried` 事件；
  终态后 `/api/session/active` 立即归零。
- 这一点比 legacy 更适合 C04：失败有确定终点，不需要靠超时猜测。

## 4. 对 C04 的直接约束

1. **持久化 session id**：v2 不能按 title 找回会话；create 也没有 title 入参。
2. **双流**：`/api/event`（delta、按 sessionID 过滤）+ durable（重放/终态）；
   重连用 `durable.seq`，不要依赖 SSE id。
3. **幂等键**：`(message_id, text, resume)`；同 id 同 text 幂等 200，不同 text 才 409。
4. **admission 前校验**：location 可执行、model 在 catalog、agent 存在；
   否则会出现“200 但静默不执行”或脏 agent。
5. **不能用 wait / legacy status**：用事件流或 `/history` 判定终态。
6. **revert 用 commit**：stage 只是标记；需要裁剪上下文时必须显式 commit。
7. **中断后保留部分文本**：UI 需要能渲染 `finish=error` 的半成品 assistant。
8. **工具裁剪只能走配置级**：v2 忽略逐轮 `tools`，会话级也没有 tools 字段。
9. **MCP 工具在 v2 不可用**：1.17.13 的 v2 工具集不含 MCP；依赖 MCP 的功能必须另找通道。
10. **失败有终态**：以 `step.failed` + assistant `finish=error` 判定，不要等 `session.error`。
11. **显式传 location**：省略时取 server cwd；C04 不应依赖进程工作目录。

## 5. 选择与回退条件

- **选择**：C04 采用 v2（进程内 `/api` 面）。依据：逐字流可用、admission 幂等可锁定、
  interrupt/revert 语义可验证、非默认 directory 事件可见、跨进程重启后 `after` 仍可用。
- **回退到 legacy + 服务端幂等**，如果后续出现以下任一情况：
  1. `/api/event` 无法按 session 过滤，或高并发下事件量不可控；
  2. `after` 在目标部署的进程重启/滚动升级下不再稳定（本套件只覆盖单机单进程重启）；
  3. interrupt/revert 的 commit 语义在目标版本发生破坏性变化；
  4. 非默认 directory 的会话事件在部署形态下不可见；
  5. 配置源隔离无法消除，且 C04 无法通过 `--pure`/环境隔离保证 model/tool 不被全局配置污染。
- 配置源隔离（`~/.opencode/opencode.json(c)` 第二条配置源）目前仍为 FAIL，是独立于选面的
  运维 gate：它不影响 v2 的功能结论，但会影响“探针/生产的 model 与 MCP 是否只来自显式配置”。
- 如果 C04 必须依赖「逐轮工具开关」或「MCP 工具」，v2 在 1.17.13 存在**能力缺口**：
  需要先设计配置级替代（每个 agent/workspace 一套工具配置），否则该部分要回退 legacy 面。
  这不属于 v2 语义破坏，但要写入 C04 的范围裁剪。

## 6. 与 legacy 套件的关系

- legacy 套件的 22 个用例保留作为历史证据，生产不提供 legacy 双路径。
- 本套件共 29 个用例：新增 v2 语义用例，并把 legacy 的逐轮 agent/model/tools/
  directory 用例改写为会话级切换或只读接口断言；P07 由“MCP 往返成功”改写为
  “MCP 在 v2 中不可用”的负向断言。
- 三连跑结果：29 passed × 3，无 flaky。

## 7. 剩余待验证（非阻塞）

- 并发 Turn（两个 session 同时跑）与 `/api/event` 的事件量/过滤成本。
- `revert/commit` 对 git 工作区 diff 的实际效果（本套件只断言消息投影）。
- 多进程/多实例部署下的 `after` 与 location 预热。
- `prompt.files` 附件与 `prompt.agents` 子 agent 列表的实际语义（当前未覆盖）。
