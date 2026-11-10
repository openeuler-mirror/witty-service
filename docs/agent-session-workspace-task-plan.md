# Agent、Session 与 Workspace：可执行任务清单

> 状态：执行稿
> 日期：2026-11-08
> 代码基线：`v2`，单一 OpenCode Runtime，模块化单体，未正式投产
> 上游方案：[agent-session-workspace-evolution-plan.md](./agent-session-workspace-evolution-plan.md)
> 物理模型：[database-model-design.md](./database-model-design.md)
> 本文只负责任务、依赖、分工和验收；架构语义与上游方案冲突时，以上游方案为准。

## 0. 执行纪律

### 0.1 两人角色

- **A＝平台/运行时负责人**：`src/witty_agent_server/**`、`src/witty_service/sandbox/**`、`src/witty_service/runtime/**`、Workspace Runtime Manager、配置投影与后台发布、三类路径、OpenCode 真实探针和真实 adapter。
- **B＝领域/产品入口负责人**：`src/witty_service/persistence/**`、`domain/**`、ORM/Repository、Session/Turn/History、Agent/Model/Skill/MCP 领域服务、`api/**`、OpenAPI、Channel、Scheduler 和前端协作。
- A 主写 Runtime seam，B 是主要消费方；B 主写 schema/API，投影、Turn Permit、恢复和错误语义由 A 负责。C04、R06/T05、E06/E07、T08/T09、E10/U08 必须共同验收。
- 热点文件先按聚合拆包再并行：A 单写 Runtime 包，B 单写领域包；不得按文件数量平均分活。

### 0.2 契约状态不是完成度标签

所有 M0、M3、M4 契约都按 **Draft → Verified Frozen** 推进：

1. **Draft**：字段、DTO、错误、测试清单和负责人已明确，允许做验证该契约所需的最薄垂直切片，不允许据此展开全量并行实现。
2. **Verified Frozen**：最薄真实 OpenCode adapter 与 Fake 通过**同一套 seam 契约测试**，对应真实探针完成且结果已落入 DTO/实现/测试；满足这些条件后可大规模并行。
3. 当前 `src/witty_service/runtime/contracts.py` 只有旧 Port/DTO 壳，OpenAPI、完整 DTO、Fake 和真实 Port 尚未实现。因此 **M0 的退出状态只能是 Draft，不得宣称 Frozen**。
4. M1 验证核心 schema、核心 API 和核心 seam；M3 验证 Agent/Skill/MCP 扩展；M4 验证入口、abort/revert/delete 和恢复扩展。后一阶段未验证的扩展仍保持 Draft。

### 0.3 清理式切换

- 新 Alembic baseline 只创建物理稿确认的新表；表数量不是目标，不为凑数增删表，实际清单以修正后的物理稿为准。
- 不保留新旧 API、新旧 schema、消息双写、兼容路由、feature flag 或旧表回退。中间未实现入口统一返回 404/501，不得接通旧实现。
- 禁止新增或保留 Agent 级能力绑定、Workspace 级能力绑定、完整执行环境快照、显式配置发布 API、持久候选配置和自动重放用户输入。
- D07 在 M1 只接受结构门禁：编译、结构测试、仓储测试和符号扫描；不得要求 M2/M3/M4 的未来 E2E。
- OpenCode 为消息唯一事实源；DB 只存领域元数据、Turn 归因、执行状态、消息 ID 归属和重生成操作状态。

## 1. 里程碑、关键路径与退出标准

| 里程碑 | 范围 | 契约门禁 | 退出标准 |
| --- | --- | --- | --- |
| **M0 契约 Draft** | C01～C05、C07、C08 | 只完成 Draft；探针可以出结果，但不得提前 Frozen | schema/API/seam/事件/错误/路径 Draft 与测试清单齐备；OpenCode 1.17.13 探针有可重复脚本和结论；首批验证切片明确 |
| **M1 新结构＋最薄真实 seam** | C06、D01～D08、R01～R05；完成 C02/C03 核心切片与 C04 核心部分的验证 | Fake 与真实 adapter 同测通过，相关探针闭环后，核心契约标 Verified Frozen；仅随后允许扩大并行 | 空库只含新表；旧 ORM/持久化删除通过结构门禁；一次真实 prompt/history 冒烟通过；两个 Workspace 的目录和进程隔离；不要求未来业务 E2E |
| **M2 直接对话、历史与发布** | R06～R09、T01～T07、T10、T11 | 核心 Turn/History API 经真实入口验证后保持 Verified Frozen | 默认 Workspace/Primary 下只选模型即可对话；切模型不改历史归因；历史无 DB 回退；保存不停服、下一 Turn 触发发布；发布失败/超时语义通过故障注入 |
| **M3 全局能力** | E01～E10、R10 | 先形成 Agent/Skill/MCP Draft；E04～E06 的最薄真实 seam、tools/Skill/MCP 探针通过后标 Verified Frozen，再展开 E07～E10 | Agent prompt 正确切换；全局 Skill 可用且同名冲突可诊断；Session MCP Activation 只影响本会话；坏 MCP 不阻塞普通对话；两个 Workspace 连接不串 |
| **M4 入口、重生成、恢复与旧 API 删除** | T08、T09、U01～U09 | 先形成入口/操作/恢复 Draft；真实 Console 垂直链路和 abort/revert/delete/恢复 seam 同测通过后标 Verified Frozen，再全面迁移 Channel/Scheduler/前端 | 三入口共用 Session/Turn；abort 幂等；最新 Turn 重生成正确；崩溃窗口可恢复可对账；旧 API 删除；P0 验收矩阵全部有证据 |

关键路径：

```text
M0 Draft
  ├─ B: C02 → D01 → D02/D03 → D04 → D05/D06/D07/D08 → C06
  └─ A: C01/C04/C05 → R01 → R02/R03 → R04/R05
                                      └─ 核心契约 Verified Frozen（M1）
M2: R05 → R06；R04 → R07；R08 → R09
    D08 → T01 → T02/T03 → T04 → T05 → T06 → T07 → T10
                         R04/R06/R07 ───┘              └→ T11（另依赖 T05）
M3: E01 → E02/E03/E04 → E05 → E06 → E07 → E08/E09 → E10
                         R10 依赖 E06，但不依赖未来恢复任务
M4: R08（已在 M2）→ T08 → T09；T05/T07/E01 也汇入 T08
    U01/U04/U06 → U05；U02/U03/U07/U08/U09 按任务表依赖收口
```

## 2. 任务总表

### 2.1 C：契约与工程基线

| ID | 任务与交付 | 验收标准 | 依赖 | 主责 | 里程碑 |
| --- | --- | --- | --- | --- | --- |
| C01 | 明确 Runtime 落位和模块边界，完善 ADR 0002；列出控制面、agent-server、sandbox 各自职责 | Workspace Runtime Manager、单 Server、单 SSE、路径和进程职责无歧义；控制面不 import agent-server 内部实现 | 无 | A+B | M0 |
| C02 | 修正物理稿并形成 schema Draft；Turn 只含 `agent_id`、`model_id` 和两个展示名快照；纳入 `session_mcp_activations`、`turn_operations` | 字段、约束、索引、UTC 毫秒、外键和级联可实施；禁止 `agent_skills`、`agent_mcp_bindings`、`execution_snapshots`；Workspace 只留 `published_projection_hash`，删除 `config_generation`；不以表数量验收 | 无 | B | M0 Draft，M1 核心 Frozen |
| C03 | 输出 OpenAPI/Pydantic DTO Draft，覆盖 Workspace、Session、Turn、Message、Agent、入口与事件 | 错误优先级固定 **400 参数 → 404 存在性 → 409 状态冲突 → 503 可用性**；请求校验不泄露默认 422；重复 abort 为 200 `already_stopped=true`；无显式发布接口；中间入口 404/501 | C02 可并行 | B | M0/M3/M4 分段 Draft、验证后 Frozen |
| C04 | 重写 `WorkspaceRuntimePort` 和 DTO；删除 `RuntimeLease`、`config_generation`，改为内存 `TurnPermit`；定义执行、事件、历史、健康、操作和恢复 DTO | Draft 明确 Permit 的“确认最新发布＋活动登记”原子语义；M1 Fake/真实 adapter 同一 seam 测试通过后才 Frozen；执行请求显式含 directory、Agent、model、Parts、完整 tools map | C01；C05 提供探针输入 | A | M0 Draft，M1/M4 分段 Frozen |
| C05 | 固定 OpenCode 1.17.13 并建立真实探针 | 覆盖逐 Turn Agent/model/directory；`prompt_async tools: Record<string,bool>` 的 sanitize、跨 Turn/Session 隔离、provider 实际工具集、skill 工具行为；同时覆盖事件、历史、revert、abort 和重启边界；结论可重复，tools 隔离失败即明确 P0 退为全局 MCP | C01 | A，B 消费结果 | M0，后续版本升级重跑 |
| C06 | 提供开发环境 reset 与幂等 bootstrap | 一条命令清理开发 DB、Runtime Home、OpenCode 状态并重建；唯一 Primary、默认 Workspace 和内建 Skill 来源就绪；重复执行无副作用 | D02、D05 | B | M1 |
| C07 | 建立质量 ratchet、迁移单头、契约测试和旧符号扫描门禁 | Ruff 清零；Mypy 新增代码零错误且存量只减不增；测试不得靠未注册 marker 或跳过假绿；密钥扫描通过 | 无 | B | M0 起持续 |
| C08 | 落实任务板、契约状态和文件所有权规则 | 每个 Ready 任务依赖完成且有验收命令；每个 Done 任务有测试与删除证据；契约变更必须同步 Fake、真实实现、OpenAPI 和探针 | 无 | A+B | M0 起持续 |

### 2.2 D：新领域结构与清理式 baseline

| ID | 任务与交付 | 验收标准 | 依赖 | 主责 | 里程碑 |
| --- | --- | --- | --- | --- | --- |
| D01 | 将 `persistence/orm.py` 按聚合拆包，只实现修正后物理稿的新表和约束 | 新 ORM 不含任何旧表兜底；Session/turn/operation/message mapping 关系、索引和级联与 C02 一致 | C02 Draft | B | M1 |
| D02 | 建立单一 Alembic 清理式 baseline 和空库升级测试 | baseline 只创建新表；空库升级、重复升级和单头检查通过；无在线数据转换和新旧 schema 并行 | D01 | B | M1 |
| D03 | 重写领域记录、状态和错误，删除旧混合模型 | Agent、Workspace、Session、Turn、TurnOperation 等记录与 C02/C03 一致；Turn 不携带执行环境正文 | C02 Draft | B | M1 |
| D04 | 将 `persistence/repositories.py` 按聚合拆分，只提供新 Repository | 无消息正文仓储和旧表读取；Session 不按 Agent-owned 路径查询；所有多表写为短事务，网络/文件 I/O 不入事务 | D01、D03 | B | M1 |
| D05 | 实现 Primary、默认 Workspace 与内建来源种子 | 模型目录可为空；无模型只引导配置；种子幂等，Primary 不归档 | D04 | B | M1 |
| D06 | 实现启动结构一致性检查 | 校验唯一未归档 Primary、唯一默认 Workspace、SQLite 外键、引用完整性和孤儿 Turn；不检查未来 Runtime 发布结果 | D05 | B | M1 |
| D07 | 在 M1 删除旧 ORM、旧持久化和不再使用的字段/分支 | **仅结构门禁**：编译、结构测试、D04 新 Repository 测试、符号扫描通过；旧调用未实现处返回 404/501；不要求未来 E2E，D08 在删除后补充完整约束矩阵 | D02、D04 | B | M1 |
| D08 | 建立新 schema 与 Repository 约束测试 | 覆盖唯一 Primary/默认 Workspace、每 Session 至多一个 running Turn、`turn_seq` 原子分配、激活默认值、操作唯一性、Mapping 幂等与级联；含并发测试 | D04、D07 | B | M1 |

### 2.3 R：Workspace Runtime、投影与发布

| ID | 任务与交付 | 验收标准 | 依赖 | 主责 | 里程碑 |
| --- | --- | --- | --- | --- | --- |
| R01 | 实现 Workspace Runtime Manager 骨架和最薄真实 adapter 落位 | 每活动 Workspace 至多一个 Server；惰性启动、健康、停止、重启和内存状态可查；支撑 M1 一次真实 prompt/history，不实现后续完整业务 | C01、C04 Draft | A | M1 |
| R02 | 将 sandbox 的 owner、进程组、端口、句柄和清理目标改为 Workspace | local_process/Docker 均以 `workspace_id` 隔离；杀 A Workspace 进程不影响 B；无 Agent 级句柄残留 | R01 | A | M1 |
| R03 | 实现 Runtime Home 和 `host_directory/runtime_directory/relative_path` 三路径 | config/data/state/cache 独立于用户目录；local/Docker 对等；拒绝 `..`、符号链接逃逸；产物在 seam 转为 Workspace 相对路径，不泄露绝对路径 | R01 | A | M1 |
| R04 | 打通真实 OpenCode client/adapter 的 session、prompt、event、history 垂直链路 | 每个请求显式携带 directory、Agent、model、Parts、完整 tools map；M1 与 Fake 跑同一 seam 测试；不得依赖隐式 Agent/model 或进程 cwd | R02、R03、C05 | A | M1；M4 由 T08 扩展操作验证 |
| R05 | 实现全量确定性 Runtime 投影器 | 每次包含全部未归档 Agent＋全部 enabled Model/Skill/MCP；稳定内部名 `ag_<definition_id>`；规范化产物和 golden 测试确定；只产生单一 `published_projection_hash` 比较值，覆盖非密配置与 `secret_revision`，不含 Session 激活或凭据本体 | D04、R03、C05 | A | M1 生成与验证，M2 发布 |
| R06 | 实现 Turn Permit 与唯一后台发布 | “确认最新投影已发布＋登记活动 Turn”原子；发布与登记互斥并等待在飞 Turn 自然结束；保存不停服，下一 Turn 触发；无显式发布 API/`publish_id`；后台以开始时冻结的非密输入执行原子写→重启→健康/可见性检查，全部成功才记录 `published_projection_hash`；HTTP 最多等 10 秒，503 时未插 running Turn且后台继续；永不自动杀 Turn；发布中再改则由下一 Turn 再触发，不自动连续重启 | R01、R05 | A，B 联调 | M2 |
| R07 | 建立每 Workspace 单 SSE 消费链和归一化事件路由 | 事件可按 Workspace/Runtime Session/活动注册或 Mapping 定位；重复事件幂等；晚到事件不推进终态 Turn；支持 `runtime_restarted/resync_required` 通知消费者全量重读 | R01、R04 | A | M2 |
| R08 | **只做**空闲停止、进程退出、端口回收和进程生命周期对账 | 空闲停止可重复；进程崩溃可被发现并标记不可用；另一 Workspace 不受影响；本任务不做控制面执行恢复，顺序必须为 R08→T09 | R01、R02 | A | M2 |
| R09 | 建立 Workspace 健康、故障隔离与可用性读模型 | Runtime 不可用时历史/产物为 503；纯 DB 列表可读并带 `runtime_status`；单 Workspace 故障不扩散 | R01、R08 | A，B 消费 | M2 |
| R10 | 完成 local/Docker 与两 Workspace 隔离集成测试 | cwd、AGENTS.md、文件、产物、事件、MCP 连接和故障均不串；路径行为对等；**不依赖 T09**，不验证未来控制面恢复 | R02、R03、R04、R06、R08、E06 | A | M3 |

### 2.4 T：Session、Turn、History 与恢复

| ID | 任务与交付 | 验收标准 | 依赖 | 主责 | 里程碑 |
| --- | --- | --- | --- | --- | --- |
| T01 | 实现 Workspace 下 Session 创建、列表、详情与 Runtime Session 惰性创建语义 | 创建时继承 Workspace 默认 Agent/model 并初始化 MCP 激活；`runtime_session_id=NULL` 时不启动 Server；列表含活动 Turn 和 `runtime_status` | C03 核心 Frozen、D05、D08 | B | M2 |
| T02 | 实现 Session 当前选择 API | Agent/model 独立修改且只影响下一 Turn；不迁移 Runtime Session、不改历史；归档/禁用校验符合 404/409 | T01 | B | M2 |
| T03 | 实现 Turn 准入、序号和活动冲突原语 | 原子分配 `turn_seq`；每 Session 至多一个 running Turn；删除和重生成可复用同一准入；冲突 409，短事务外不做 I/O | D08、T01 | B | M2 |
| T04 | 实现 **Turn Attribution** | Turn 只固定 `agent_id`、`model_id` 和两者当时展示名；目录改名后历史标签不变；不得据此重建执行环境 | T03 | B | M2 |
| T05 | 实现 Turn 执行管线并接入 Turn Permit | 最终短事务复核后插 running；显式传 directory/Agent/model/Parts/tools map；获取 Permit 失败或 10 秒超时不插 running；prompt 不自动重试，断连不取消；所有出口释放 Permit，事件只低频写关联与终态 | T03、T04、R04、R06、R07 | B，A 联调 | M2 |
| T06 | 实现 `turn_runtime_messages` 消息归属 | Runtime Message ID 与 ordinal 幂等唯一；不保存正文、Parts、Delta 或 Tool 副本；晚到 Delta 不复活终态 | T05 | B | M2 |
| T07 | 实现 History Projection 和 Session 历史/事件读模型 | 只读 OpenCode 并合并 Turn 归因；只使用同一 Runtime Session 原生游标；非法/跨 Session 游标 400；不可用 503而非空历史；DB 列表带 `runtime_status`；重同步事件要求全量重读 | T06、R04、R09 | B | M2 |
| T08 | 实现幂等 abort、最新 Turn 重生成和 Session 删除 | abort 不存在/跨 Session 404，重复 200 `already_stopped=true`；running Turn 收到归一化 abort 结果后条件更新终态，工具状态与 OpenCode 一致，晚到 Delta 不推进；重生成仅最新 Turn，先复核无活动 Turn 和 Runtime Session 未变化，从 Runtime 读取原 Turn 用户 Parts；使用原 Turn Agent 最新配置＋Session 当前模型，模型不存在 404、禁用 409，检查原 Turn Agent 归档而非 Session 当前 Agent；先写 `reverting`，取得 Permit 后 revert，失败写 `failed` 且不新建 Turn，revert 成功后新建 running Turn，被接受后写 `accepted`；Turn 不保存自引用重生成字段；`turn_operations` 仅 `reverting/accepted/failed/interrupted` 且不存正文，禁止 `reverted/superseded` 状态；删除先写 `deleted_at` 关准入，再收敛活动 Turn、删 Runtime Session、硬删领域记录，清理失败保留删除标记且可重试、不重开准入 | T05、T07、E01、R08 | B，A 提供真实操作 adapter | M4 |
| T09 | 实现控制面 fail-stop 恢复与重生成对账 | **在 R08 后实施**；控制面重启不续跑、不重放，不明 running 标 `interrupted`；旧执行收敛前关闭准入；恢复不能凭操作记录断定 revert 已发生，必须对账 Runtime 可见历史并修复 `last_turn_seq`；恢复后历史/产物不可用仍 503，SSE 要求全量重读 | R08、T05、T08 | B，A 联调 | M4 |
| T10 | 删除剩余 DB 消息写入、消息 Repository 调用、compare 和跨源游标代码 | 主路径只走 Runtime 历史；T07 的 400/503 与无回退测试通过；符号扫描无残留；**只依赖 T07，不依赖 T11** | T07 | B | M2 末尾 |
| T11 | 完成零配置直接对话与模型切换 E2E | 不创建 Expert 即可对话；同 Session 切模型后下一轮生效且旧归因不变；无 DB 历史回退；**依赖 T05/T07，与 T10 无循环** | T05、T07 | A+B | M2 |

### 2.5 E：Agent 与全局能力

M3 先把 E01/E03/E06 的 API、DTO、tools 和错误扩展标为 Draft；E04～E06 用真实 OpenCode 与同一 seam 验证后，才将这组契约标为 Verified Frozen 并展开后续并行。

| ID | 任务与交付 | 验收标准 | 依赖 | 主责 | 里程碑 |
| --- | --- | --- | --- | --- | --- |
| E01 | 实现单层 Agent Definition 编辑与校验 | 仅名称、描述/头像、system prompt、来源和归档信息；Primary/Expert 同一执行路径；禁止 Agent `default_model_id` 和能力绑定；原地保存 | D04、C03 M3 Draft | B | M3 |
| E02 | 实现 Agent 乐观锁 | 只使用严格递增的 `config_updated_at`；并发更新 409；禁止 `config_hash`，不生成配置版本 | E01 | B | M3 |
| E03 | 实现 Session MCP Activation 领域与 API | 新 Session 按当时 `default_active` 初始化；已有 Session 不随后续默认值或新增 MCP 改变；修改只影响本会话后续 Turn，不触发发布；探针失败时不开放 Session 开关，明确退为全局 MCP | T01、D04、C05 | B | M3 |
| E04 | 验证并补齐全量 Agent Definition 投影 | 所有未归档 Definition 全量投影，稳定 `ag_<definition_id>`；编辑不改变内部身份、不增加按编辑次数命名的文件；真实 provider 收到指定 Agent 和最新 prompt | R05、E01 | A | M3 |
| E05 | 实现全局 Skill 目录、物化与冲突治理 | 所有 enabled Skill 对所有 Agent 可见；全局同名拒绝并指出冲突项；删除旧 deny-first 白名单及 Workspace 覆盖逻辑；真实 Skill 调用成功 | E04、D04 | A，B 提供目录服务 | M3 |
| E06 | 实现全局 MCP、Workspace 连接和本轮 tools 过滤 | 仅 Session active＋全局 enabled＋连接 ready 的工具暴露；每轮传完整 tools map，名称采用 C05 实测 sanitize；跨 Turn/Session 无残留且 provider 实收一致；连接/调用超时、退避、熔断齐备，坏 MCP 不阻塞启动或普通对话；失败降级不得改成临时 Agent/Workspace 能力绑定 | E03、E05、R05、T05、C05 | A，B 联调 | M3 |
| E07 | 将 Agent/Skill/MCP/Model 保存接入“下一 Turn 发布” | Agent 保存只更新定义并推进 `config_updated_at`，其他目录只更新自身领域值；所有保存均不停服、不暂停当前准入；下一 Turn 复用 R06 唯一后台发布；在飞 Turn 自然完成；发布失败不以旧投影执行新 Turn，发布中再改不自动连续重启 | E02、E04、E05、E06、R06 | A+B | M3 |
| E08 | 打通同 Session 切 Agent 与历史归因 | Primary→Expert A→Expert B→Primary 上下文连续；每轮使用所选 Agent 最新配置；历史始终按 T04 的 Turn 归因展示，不被 Session 当前值改写 | T04、T07、E04、E06、E07 | B，A 联调 | M3 |
| E09 | 实现归档/禁用、默认值回退和入口暂停 | Workspace 默认 Agent 归档回退 Primary，已有 Session 保留当前值且新 Turn 409；默认模型不可用时选下一 enabled 候选模型或置空；Channel/Scheduler 进入 `paused + reason`，恢复必须显式重选 Agent/model；历史归因保留 | E01、D04、T02 | B | M3 |
| E10 | 完成全局能力与隔离验收 | 全局目录对 Agent 一致；Session 激活只影响本会话；两个 Workspace 的 MCP 连接和故障不串；坏 MCP 可普通对话；保存→下一 Turn 发布与在飞 Turn 隔离通过；若 C05 失败则验收全局 MCP 降级且无假 Session 开关 | E05、E06、E07、E08、E09、R10 | A+B | M3 |

### 2.6 U：Console、Channel、Scheduler 与旧入口删除

M4 先由 T08/T09、U01～U03 形成入口/操作/恢复 Draft，仅实现一条最小真实 Console 垂直切片，并以同一 seam 的操作/恢复测试完成 Verified Frozen；此后才扩展其余前端功能并全面并行迁移 Channel 和 Scheduler。

| ID | 任务与交付 | 验收标准 | 依赖 | 主责 | 里程碑 |
| --- | --- | --- | --- | --- | --- |
| U01 | 实现 Workspace CRUD API（首版不删除） | host directory 规范化、去重、符号链接解析；默认值只影响新 Session；错误符合 C03 | D05、C03 核心 Frozen | B | M4 |
| U02 | 完成 Console Workspace/Session 体验 | Workspace 聚合 Session；Agent/model 独立选择并提示下一轮生效；展示 Turn 归因、发布等待和 Runtime 状态 | U01、T02、T07、E08 | B（前端协作） | M4 |
| U03 | 迁移前端 API、SSE 与重同步 | 全部改用新 Session/Turn/Message API；400/404/409/503 可诊断；收到 `runtime_restarted/resync_required` 后全量重读 Turn 与历史，不续放旧 Delta | U02、T01、T07、T08 | B（前端协作） | M4 |
| U04 | 将 Channel Instance/Provisioning 绑定改为 Workspace＋Agent＋Model | 扫码与手填凭据保存相同意图；归档/禁用进入 `paused + reason`，恢复需显式重选；凭据语义不变 | U01、D04、E09 | B | M4 |
| U05 | 将 Channel 路由接入统一 Turn Service | 按 Workspace 创建/复用 Session；不旁路 Turn Permit；保留去重、占位、延迟补发和投递确定性；运行记录关联正确 Session/Turn | U04、T05、T08 | B，A 联调 | M4 |
| U06 | 迁移 Scheduler 绑定与运行记录 | 使用 `workspace_id + agent_id + model_id`；复用统一 Turn Service；保留 cron/interval、去重和停滞检测；暂停/恢复语义同 E09 | U01、T05、E09 | B | M4 |
| U07 | 清理 OpenAPI、DTO、错误和产物文件 API | 实现与 OpenAPI 一致；统一 400→404→409→503；前端不传 OpenCode 内部名、凭据或绝对路径；文件 API 防路径逃逸 | T08、U03、U05、U06 | B | M4 |
| U08 | 自动化 P0 验收矩阵并归档证据 | 上游每个 P0 场景均有自动化测试或可重复脚本；故障注入不得标成 I05 的未来工作；每项有 owner、命令和结果 | T09、T11、E10、R10、U03、U05、U06、U07 | A+B | M4 |
| U09 | 删除旧 Agent-owned Session API、旧消息 API、旧 DTO 和旧调用 | 新入口验收通过后删除；旧路由 404；前端/Channel/Scheduler 无旧调用；符号扫描为零，无兼容分支 | U03、U05、U06、U07 | B | M4 |

### 2.7 I：P1 增强（不得反向阻塞 P0）

| ID | 任务 | 依赖 | 主责 |
| --- | --- | --- | --- |
| I01 | Agent 模板导入/导出，仅包含定义字段和来源，不包含能力授权 | E02、E05、E06 | B |
| I02 | 展示后台发布状态、等待中的活动 Turn 和非密投影差异 | R06、E07 | B |
| I03 | Workspace 归档，不删除历史 | U01 | B |
| I04 | 可配置空闲策略与资源观测 | R08、I07 | A |
| I05 | **扩展 soak**：长时间、随机顺序和高并发重复 P0 故障注入；不得承接或推迟任何 P0 必测项 | M4 全部 P0 故障测试已通过 | A+B |
| I06 | 有真实资源证据后评估单 Server 多 directory | R10、I09 | A |
| I07 | 全链路非密日志与指标：冷启动、准入、首事件、发布、历史、MCP | M2 起增量实施 | A+B |
| I08 | 路径越界、凭据、日志和投影泄露审计 | R03、R05、E06 | A |
| I09 | 冷启动、切模型、发布、历史分页性能基线 | M4 | A |
| I10 | 更新部署、reset、排障和 OpenCode 升级 Runbook | M4 | A+B |

P2 的 Team 仍单独立项；P0 不创建 Team 表、通用 Outbox 或协作编排，不占当前关键路径。

| ID | 任务与验收 | 依赖 | 主责 |
| --- | --- | --- | --- |
| M01 | 用独立探针验证 OpenCode task/parent/children 能否表达拆解、上下文、预算、停止恢复与聚合，形成 go/no-go 证据 | P0 完成 | A+B |
| M02 | 定义 Team、角色、任务、消息、预算、审计及与 Session 切 Agent 的边界，定稿后才允许原型 | M01 | A+B |
| M03 | 在隔离原型中验证并发、失败恢复、聚合和审计，不改 P0 主链路 | M02 | A+B |
| M04 | 形成独立 ADR、资源和数据模型及迭代计划；未通过则不建表 | M03 | A+B |

## 3. 删除门禁与符号扫描

### 3.1 两级门禁

| 层级 | 时点 | 必需证据 | 明确不要求 |
| --- | --- | --- | --- |
| **L1 结构删除** | D07（M1）及后续局部删除 | 编译、结构测试、新 Repository/约束测试、迁移单头、符号扫描；未实现入口 404/501 | D07 不得要求 T11、E10、U08 等未来 E2E |
| **L2 行为收口** | T10（M2）、U09（M4）与 M4 总验收 | 对应新路径的真实 seam/业务验收、入口迁移、无回退、无旧调用、符号扫描 | 不以“以后补测试”替代本里程碑验收 |

### 3.2 删除清单

| 删除对象 | 新边界/结果 | 门禁与归属 |
| --- | --- | --- |
| `messages`、`message_events`、DB 消息正文持久化、compare | OpenCode 历史＋Turn 归因＋Runtime Message ID 归属 | D07 做 L1；T10 在 T07 无回退验证后做 L2 |
| `agent_runtime_state`、`agent_locks` | Runtime 内存状态＋Turn 准入约束 | D07/D08 结构与并发测试 |
| `agent_skills`、`agent_mcp_bindings`、`execution_snapshots` 及旧 revision 表 | 全局 Skill/MCP＋Session Activation＋Turn Attribution | C02/D01/D07 禁止出现，符号扫描为零 |
| `RuntimeLease`、`config_generation`、Agent `config_hash` | 内存 Turn Permit＋单一 `published_projection_hash`＋`config_updated_at` | C04/D07 删除；R06/T05 行为验证 |
| 显式发布 API、`publish_id`、持久候选配置 | 保存后由下一 Turn 触发唯一后台发布 | C03/R06/U07 禁止出现 |
| Session `runtime_type`、`runtime_session_key`、`remote_runtime_agent_id` | 单 OpenCode＋`runtime_session_id` | D07 结构删除；R04/T05 显式 directory 验证 |
| Agent-owned Session 路由和旧消息 API | Workspace/Session/Turn/Message 新 API | U09 在新入口验收后删除 |
| Agent 级 sandbox/server 和通用 Runtime 分支 | Workspace 级单 OpenCode Runtime | R02/R10 隔离验证，D07 清理持久化残留 |
| Skill deny-first、Agent/Workspace 能力覆盖与 MCP 同名覆盖逻辑 | 全局 Skill＋全局 MCP＋Session tools 过滤 | E05/E06 删除并由真实调用验证 |

符号扫描至少覆盖上述旧对象、旧路由、旧 DTO 和旧字段；结果只允许出现在“删除/禁止”说明或历史方案中，不得出现在 `src/`、可执行测试 fixture 或新 OpenAPI 中。

## 4. 测试与 P0 故障注入

### 4.1 固定测试层

- **每次提交**：领域/仓储单测、SQLite 约束与并发、DTO/OpenAPI golden、Fake seam、路径、投影 golden、符号扫描。
- **真实 Runtime**：同一 seam 契约套件同时运行 Fake 与 OpenCode adapter；版本固定 1.17.13，缺版本或探针失败必须显式失败。
- **里程碑集成**：M1 只要求结构与最薄真实 prompt/history；M2/M3/M4 分别验收对话发布、全局能力、入口操作恢复，不能把未来 E2E 前移。
- **安全与日志**：不记录完整 prompt、正文、凭据或宿主敏感路径；日志贯穿 Workspace/Session/Turn/Runtime Session ID 和投影 hash。

### 4.2 P0 故障注入必须随对应里程碑完成

| 里程碑 | 故障与操作 | 必须断言 | 归属任务 |
| --- | --- | --- | --- |
| M1 | 杀掉 Workspace A 的 Server | A 被识别不可用，B 的目录/进程/健康不受影响；不做未来业务恢复承诺 | R01～R04 |
| M2 | 投影写入、重启或健康检查失败 | 触发请求 503，未插 running Turn；不以旧投影执行新 Turn；后台有确定结果；其他 Workspace 可用 | R06/T05 |
| M2 | 发布等待超过 10 秒、发布中再次修改、在飞 Turn 未结束 | 10 秒只是 HTTP 等待上限；不自动杀 Turn、不取消后台发布；当前发布后不自动连续重启，下一 Turn 再处理差异 | R06/T05 |
| M2 | Runtime 进程崩溃/重启 | 不续跑、不重放；历史/产物 503；纯 DB 读带 `runtime_status`；SSE 通知全量重读；另一 Workspace 正常 | R07～R09/T07 |
| M3 | MCP 连接失败、调用超时、持续故障 | 非 ready 工具不进入本轮 map；普通对话可用；超时/退避/熔断生效；跨 Session/Turn/Workspace 不串 | E06/E10/R10 |
| M3 | Agent/模型归档禁用与入口引用竞争 | 默认回退正确，已有 Session 当前值保留；Channel/Scheduler `paused + reason`；恢复必须显式重选 | E09/E10 |
| M4 | revert 成功后、新 Turn accepted 前杀控制面 | 操作标 `interrupted`；不自动重试；对账 Runtime 可见历史并修复 `last_turn_seq`；用户可重新输入 | T08/T09 |
| M4 | 控制面在 running、prompt 结果不明时崩溃 | fail-stop；不续跑、不重放；不明 running 为 `interrupted`；收敛前不开放准入 | T09 |
| M4 | Session 删除与新 Turn/晚到事件竞争 | `deleted_at` 先关闭准入；无新 running 逃逸；清理失败可重试且不重新开放；旧事件不投递 | T03/T08 |
| M4 | 重复 abort、revert 失败、非最新 Turn 重生成 | 重复 abort 200 `already_stopped=true`；revert 失败不新建 Turn；非最新目标 409；错误优先级稳定 | T08/U07 |

I05 只扩展上述用例的持续时间、随机顺序和并发强度；任何一项都不得被标记为“P1 再做”。

## 5. 首批开工任务（人话、位置、交付结果）

以下任务均可从 `v2` 直接开始。第一批只形成 Draft 和验证工具，不把接口壳或 Fake 单独通过当作 Frozen。

| 顺序 | 人要做什么 | 具体代码/文档位置 | 做完得到什么 | 负责人 |
| --- | --- | --- | --- | --- |
| 1 | 把 Runtime 到底放在哪、谁管进程/配置/事件写清楚，避免双方各写一套 Manager | `docs/adr/0002-workspace-runtime-control-plane-seam.md`；对照 `src/witty_service/runtime/contracts.py`、`src/witty_agent_server/application/services/agent/opencode_lifecycle_service.py` | C01：完整 ADR，含模块职责、调用方向、保留/删除清单和 M1 最薄链路 | A |
| 2 | 先把物理稿改成新决策，后续 ORM 才能一次写对 | `docs/database-model-design.md` | C02 Draft：新字段/约束/索引/级联；Turn 仅归因和两个展示名；包含 activation/operation，Workspace 仅有单一发布 hash；无凑表数要求 | B |
| 3 | 把控制面调用 Runtime 的 Python 契约改成 Turn Permit，并列出 Fake/真实实现要共同通过的测试 | `src/witty_service/runtime/contracts.py`；新增契约测试放在 `tests/runtime/test_workspace_runtime_contract.py`；真实 client 对照 `src/witty_agent_server/infra/clients/opencode_client.py` | C04 Draft：Port、Permit、执行/事件/历史/健康 DTO 和测试清单；此时不交“已冻结”结论，M1 才补 Fake/真实同测 | A |
| 4 | 把前端和三入口要调用的 URL、请求响应、错误码一次列清 | `src/witty_service/api/` 的 DTO/错误模块；OpenAPI Draft 固定放 `docs/agent-session-workspace-openapi.yaml` | C03 Draft：Workspace/Session/Turn/Message/Agent 核心接口，M3/M4 扩展明确仍 Draft；含 400→404→409→503 和幂等 abort 示例 | B |
| 5 | 用真实 OpenCode 回答 tools 和 Skill 能否按 Session 隔离，不靠猜工具名 | 固定版本与探针放 `tests/probes/test_opencode_1_17_13.py`，结果记录放 `docs/probes/opencode-1-17-13.md`；调用 `opencode_client.py` | C05：可重复探针及通过/失败矩阵，覆盖 sanitize、完整 tools map、provider 实收、跨 Turn/Session、Skill、事件/历史/revert/abort；失败即采用全局 MCP 降级结论 | A |
| 6 | 把质量门禁做成后续两人都能跑的同一把尺子 | `pyproject.toml`、`tests/`、现有 CI 配置目录 | C07：Ruff/Mypy ratchet、迁移单头、契约测试和符号扫描的可执行入口与当前基线 | B |
| 7 | 确认文件 ownership 和两条 M1 验证切片，避免 Draft 后全面铺开 | 本文件“角色”“里程碑”“任务总表”；代码落点以 D01～D08、R01～R05 为准 | C08：A 负责 R01→R04 最薄真实链路，B 负责 D01→D08 新结构，R04/C04 的 Fake 与真实 adapter 同测通过后标记核心 Frozen | A+B |

**下一批而不是首批并行扩张**：M0 Draft 完成后，A 只做 R01～R05 验证切片，B 只做 D01～D08 与 C06；核心 Verified Frozen 前，不开始全量 Channel/Scheduler/前端迁移，也不把未实现入口接回旧路径。

## 6. 依赖自检与完成定义

- T10 只依赖 T07；T11 只依赖 T05/T07，二者可先后或并行，**不存在 T10↔T11 循环**。M2 退出同时要求两者完成。
- R10 只依赖已完成的 Runtime/发布/隔离任务和 E06，不依赖 T09；R08 在 M2，T09 在 M4，顺序固定为 R08→T09。
- E09 不依赖未来 U04/U06：M3 先在新 schema/领域服务上实现暂停规则，M4 入口任务消费并做端到端验证。
- R06 不依赖未来 T05：A 先用 seam/Fake 验证 Permit 与唯一后台发布，T05 再接入并证明 503 未插 running Turn。
- 每个任务 Done 必须同时有：实现、对应测试、验收证据、必要的删除扫描；涉及契约时还必须有 Draft/Verified Frozen 状态。
