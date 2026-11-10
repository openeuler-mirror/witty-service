# OpenCode 1.17.13 真实探针结论

| 项 | 值 |
| --- | --- |
| 日期 | 2026-11-10 |
| 分支 | `feat/c03-api-contract`（基于 `42a86e5`，探针文件尚未提交） |
| 被测版本 | **1.17.13**（固定；npm 包身份 `opencode-ai@1.17.13`） |
| C05 状态 | **Draft**（M0，不提前 Frozen） |
| 核心口径 | L1 22 个 + L2 4 个 required = 26 个；L2 optional 4 个、L3 真实 LLM 2 个为可选证据 |
| 对照实现 | `src/witty_agent_server/infra/clients/opencode_client.py` |

> 本文只记录**实测事实**。覆盖表不能替代结果矩阵；没有“文档声称、测试没做”的项。

## 1. 版本固定与安装

本机 `PATH` 上的 `opencode` 曾漂移到 1.17.20，探针不复用系统副本，单独安装一份：

```bash
mkdir -p /opt/witty-service/opencode-1.17.13
cd /opt/witty-service/opencode-1.17.13
npm init -y
npm install --save-exact opencode-ai@1.17.13
```

可执行文件是 `node_modules/opencode-ai/bin/opencode.exe`（1.17.13 的 npm 包只有
`.exe` 这个名字）。探针按以下优先级解析，**版本或包身份不符直接失败，不 skip**：

1. `WITTY_PROBE_OPENCODE_BIN=/绝对路径/opencode.exe`
2. `WITTY_PROBE_OPENCODE_DIR=/opt/witty-service/opencode-1.17.13`（自动拼 npm 布局）
3. `PATH` 中的 `opencode.exe` / `opencode`（兜底，最容易拿到错误版本）

校验链：`opencode --version == 1.17.13` → 向上查找 `package.json` 校验
`name == "opencode-ai"`、`version == "1.17.13"` → `GET /global/health.version`
一致。任何一环不满足，fixture 直接 `fail`。

## 2. 探针方法（当前 harness）

- **真实 OpenCode 进程**：每个测试独立 session + 唯一 marker；restart 用例使用
  独立 server 实例，不中途重启共享 env。
- **mock provider**：`tests/probes/mock_provider.py` 起 OpenAI 兼容服务，
  `Transfer-Encoding: chunked` 逐块 flush；支持多 chunk 文本、`tool_calls`、
  500、timeout、慢流。断言按 marker 锚定（`requests_for` / `request_for`），
  不使用 `requests[-1]` 或顺序计数。
- **MCP**：`mcp_echo_server.py` 提供 `echo_upper` / `echo_lower` / `slow.echo`，
  启动后轮询 `/mcp` 直到 `probe.status == "connected"`。
- **隔离**：独立 `HOME` / `XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_STATE_HOME` /
  `XDG_CACHE_HOME`；关闭外部技能扫描
  （`OPENCODE_DISABLE_EXTERNAL_SKILLS=1`、`OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1`）；
  `share:"disabled"`。
- **进程**：`start_new_session=True`，停止时 `os.killpg` SIGTERM/SIGKILL，
  按 `run_id` 扫描 `/proc` 清理孤儿；默认清理临时目录
  （`WITTY_PROBE_TMP_ROOT`，默认 `~/.cache/witty-probe`，避免写满 `/tmp`）。
- **事件**：`EventCollector` 解析 `id:` / `event:` / `data:`，支持 `directory`
  与 `after`，`stop()` join 线程并把异常抛回测试。

## 3. 结果矩阵

状态取值：`通过` / `失败` / `部分` / `未测`。证据列指向测试文件与关键断言。

### 3.1 L1 能力探针（22）

| 要求 ID | 测试 ID | 期望 | 实际 | 证据 | 影响 |
| --- | --- | --- | --- | --- | --- |
| P01 | `test_p01_version_and_package_identity` | `--version`、包身份、health 均为 1.17.13 | 通过 | 二进制 `--version`、`package.json`、`/global/health` 三处一致 | 版本漂移会直接失败，不再出现 1.17.20 假绿 |
| P02 | `test_p02_per_turn_agent` | probe-a → probe-b → 省略回 build，system 不粘滞 | 通过 | 三轮 provider system 分别含 A / B / `You are opencode` | Agent 逐 Turn 覆盖，省略即回默认 `build` |
| P03 | `test_p03_per_turn_model` | 显式 model 生效并粘滞；显式切回生效；不串 Turn | 通过 | 第 2 轮省略仍为 `mock-model-b`；显式后回到 `mock-model`；新 session 不继承 | model 是**会话级粘滞**，必须显式下发 |
| P04 | `test_p04_per_turn_directory` | 各自 `AGENTS.md` 进入对应 system；`session.directory` 一致 | 通过（含 1 条行为记录） | A/B token 互斥；`session.directory` 正确。带错 directory 时**返回 204**，仍按 session 自身目录解析，B 的 token 未串入 A | directory 在会话创建时固定；prompt 侧参数不改变解析目录 |
| P05 | `test_p05_default_toolset_and_sanitize` | provider 工具集合**等于** baseline；无原始 MCP 名 | 通过 | baseline = 11 内建 + `probe_echo_upper/lower/slow_echo`（共 14）；无 `echo_upper`/`slow.echo` | sanitize 规则 = `server名_工具名`，非字母数字替换为 `_` |
| P06 | `test_p06_tools_map_stickiness` | 禁用后省略仍禁用；显式 true 恢复；新 session 全量 | 通过 | 四步集合精确比对 | `tools` 是会话级粘滞；省略 ≠ 重置 |
| P07 | `test_p07_mcp_roundtrip` | `/mcp` connected；调用 `probe_echo_upper` 得到大写结果 | 通过 | 第二次 provider 请求的 tool result 含 `PROBE` | 本地 stdio MCP 可调用 |
| P08 | `test_p08_skill_exposure` | `/skill` 含探针 skill；可调用；`skill:false` 后消失 | 通过 | tool result 含 `PROBE_SKILL_BODY_7f3a`；禁用后集合不含 `skill` | `skill` 属内建工具，可按 Turn 禁用 |
| P09 | `test_p09_revert_cuts_history` | revert 后下一轮历史从该 user 前截断 | 通过 | revert 后 `GET message` 仍有 4 条；下一轮历史只剩 marker1/marker3，`session.revert` 清空 | revert 是暂存态，真正的历史修剪发生在下一 Turn |
| P10 | `test_p10_unrevert_restores_history` | unrevert 恢复完整历史 | 通过 | `session.revert` 消失；下一轮含 marker1/2/3 | 恢复走 `unrevert` |
| P11 | `test_p11_abort_keeps_interrupted_message` | busy 时 abort；status 消失；`session.error`；被中断 assistant 仍在历史 | 通过 | `session.error` 文本含 `abort`；assistant 消息带 `MessageAbortedError`；status 中无该 session | abort 不是“无痕”，历史保留中断消息 |
| P12 | `test_p12_event_stream` | delta / part.updated / message.updated / session.idle；finish 正确 | 通过 | 事件类型齐备；`message.part.delta` 的 delta 非空；最终 `finish=stop` | 事件可用于投影 |
| P13 | `test_p13_event_scope_and_delta` | 默认目录只见默认 session；指定 directory 才见目标；delta 逐块 | 通过 | 三个 collector 的 sessionID 互不越界；`"".join(deltas) == "PROBE_OK"` | 事件订阅是 location 作用域，控制面必须带 directory |
| P14 | `test_p14_restart_boundary` | 重启后历史/model/tools 保留，可继续 | 通过 | 重启后 4 条消息 ID 不变；续跑请求 model 仍 `mock-model-b`、`probe_echo_upper` 仍禁用 | 状态在 XDG data 目录内持久化 |
| P15 | `test_p15_restart_id_stability` | 重启前后 message/part ID 可关联 | 通过 | message/part ID 序列完全一致；新 Turn 的 ID 与旧集合不相交 | 重启不会重新生成 ID，可用于关联 |
| P16 | `test_p16_tools_boundaries` | `{}`、未知 key、原始名、未暴露工具、false/true 组合语义明确 | 通过（含 1 条语义记录） | 10 组精确集合比对。`{}`/未知 key/原始 MCP 名均等于全量；显式 map 是**整体覆盖**，未指定的 key 恢复默认 true | 契约必须每轮下发**完整 tools map**，不能依赖“部分覆盖” |
| P17 | `test_p17_default_directory` | 不传 directory 时 session.directory 与 server cwd 一致；pwd 落点正确 | 通过（supporting） | `session.directory == GET /path.directory`；`bash pwd` 结果含该目录 | 默认目录行为可预期 |
| P18 | `test_p18_invalid_agent_and_model` | 非法 agent/model 明确报错，不静默串上一 Turn | 通过（含 1 条高风险记录） | 非法 agent：204 + `session.error: Agent not found`，不落 user 消息；非法 model：204 + `Model not found`，不调用 provider，**但非法 model 会写进 session**，之后省略 model 仍失败，需显式合法 model 恢复 | C04/M2 必须在应用前校验 model 并返回 404，避免污染会话 |
| P19 | `test_p19_provider_failure_is_bounded` | 有界重试，不无限挂起 | **部分（PARTIAL，xfail）** | 1.17.13 对 provider 500/断连只进入无限 retry：`status.type == "retry"`、attempt 递增、`next` 拉长；120s 内不产生 `session.error`，也不自动终止 | R04 必须由调用方自建超时/熔断；不能等 OpenCode 报错 |
| P20 | `test_p20_legacy_message_id_bounded` | 正常路径每轮 1 次请求；自定义 messageID 有界 | 通过（supporting） | 正常路径 `count == 1`；自定义 `msg_...` 在 10s 内请求数 ≤ 6，超限则 abort 并失败 | legacy messageID 不能满足严格幂等，需 watchdog |
| P21 | `test_p21_cross_session_isolation` | A 禁用不影响 B，反向也成立 | 通过 | 双向多步集合比对：A 禁用 upper 时 B 全量；B 禁用 lower 时 A 恢复 upper 不影响 B | tools 隔离成立，**不需要把 P0 退化为全局 MCP** |
| P22 | `test_p22_history_shape_and_pagination` | 消息顺序、parts 形状、`limit/before` 原生支持、未知/非法 session | **部分（PARTIAL）** | 顺序实测为**时间正序**（计划原文写“新→旧”，以实测为准）；parts 形状正确；`limit` 原生生效（返回最近 N 条、仍正序；`limit=0/100` 等于全量）；`before` 对合法 message ID 返回 **400**，分页不可用；非法形状 session ID 返回 **500** | C04 历史/分页不能依赖 `before`；需在应用层实现游标，并对非法 session 做入参校验 |

### 3.2 L2 client 冒烟（8）

| 要求 ID | 测试 ID | 期望 | 实际 | 证据 | 影响 |
| --- | --- | --- | --- | --- | --- |
| C1（required） | `test_c1_create_and_stream_one_turn` | create → stream 一轮；delta/terminal；不挂起 | 通过 | 收到 `message.part.delta` 与终止事件；`resolve_session_id` 一致 | 生产 `stream_turn` 单轮可用 |
| C2（required） | `test_c2_two_turns_same_session` | 同 session 连续两轮；不把 ghost 终止当本轮结束 | 通过 | 两轮均收到 delta 与 terminal；provider 请求数各 ≥ 1 | 多轮消费正确 |
| C3（required） | `test_c3_abort_while_running` | 运行中 abort；以 error/终止事件收尾；SSE 不泄漏 | 通过 | abort 后线程退出；事件含 `session.error` 或终止事件 | abort 路径可用 |
| C8（required） | `test_c8_server_killed_during_stream` | 流中杀 server/断 SSE 必须抛错退出 | 通过 | `stream_turn` 抛错并退出，未无限等待 | 断连不会静默挂死 |
| C4（optional） | `test_c4_idle_abort_and_unknown_session` | 空闲 abort 是 no-op；未知 session 可识别 | 通过 | 空闲 abort 成功；未知 session 抛 `OpenCodeClientError(status=409)` | 错误形态可识别 |
| C5（optional） | `test_c5_delete_then_stream` | 删除后再 stream 报错明确 | 通过 | 抛出 `status=409` | 删除后不会误用旧映射 |
| C6（optional） | `test_c6_duplicate_title_ambiguous` | 同名 title 的歧义可识别 | 通过 | `find_session_id` 抛 409，消息含 `ambiguous` | 重名不会被静默选错 |
| C7（optional） | `test_c7_restart_then_continue` | restart 后按 title 重解析，历史还在 | 通过 | 新 client 解析到同一 session ID；历史为 4 条（user/assistant ×2） | 内存 mapping 失效后可恢复 |

### 3.3 L3 真实 LLM 冒烟（可选，不属于必跑门槛）

| 要求 ID | 测试 ID | 期望 | 实际 | 证据 | 影响 |
| --- | --- | --- | --- | --- | --- |
| R1 | `test_r1_real_text_streaming` | 真实 provider 文本流式；finish=stop；至少 1 个 delta；无 error | 通过 | DeepSeek `deepseek-flash` 返回非空 assistant、含 `PROBE`、`finish=stop`、有 delta | 真实鉴权与 SSE 可用 |
| R2 | `test_r2_real_tool_call` | 明确要求调用 `probe_echo_upper`；结果回填 | 通过 | 模型实际产生 tool part，`state.status == completed`，output 含 `PROBE` | 真实模型路径下 MCP 工具可调用 |

> key 仅通过环境变量 `WITTY_PROBE_REAL_API_KEY` 传入，未写入仓库、日志、fixture 或失败快照。

## 4. 关键结论

### 4.1 tools 隔离成立，但必须每轮下发完整 map

- 按 Turn 禁用 MCP 工具生效，跨 Session 无残留，P21 双向成立 → **不需要把 P0 退化为“全局 MCP”**。
- 但 `tools` 是**会话级粘滞状态**，且显式 map 是**整体覆盖**而非逐 key 合并：
  - 省略 `tools` → 沿用上次；
  - 显式 `true` → 恢复；
  - 显式 map 未列出的 key → 恢复默认 `true`；
  - 该状态跨进程重启保留。
- 因此 `TurnExecutionRequest.tools` 必须始终是**完整 map**，“省略即默认”不成立。

### 4.2 Agent 与 model 语义不对称

- Agent：逐 Turn 覆盖，**省略回默认 `build`**，不粘滞。
- model：逐 Turn 覆盖，**省略沿用上一次**，且跨重启保留。
- 两者都必须在执行请求中显式携带，不能依赖 OpenCode 缺省行为。

### 4.3 directory 是位置维度

- 会话创建时的 `directory` 固定；`AGENTS.md` 进入该会话 system prompt。
- prompt 侧传错 directory 不会切换目录（实测 204，仍按 session 自身目录解析）。
- 事件订阅与 `/session/status` 是 location 作用域；不带 directory 观测不到其它目录的会话。
- C04 健康/事件/历史 DTO 必须能表达“按 directory 查询”。

### 4.4 revert / abort / 重启

- `revert` 只写暂存标记，不删历史；历史修剪发生在下一 Turn；恢复走 `unrevert`。
- `abort` 后 session 从 status 消失并发 `session.error`；被中断的 assistant 消息保留（`MessageAbortedError`）。
- 单实例重启不丢 Session/消息/model/tools，message/part ID 稳定；R01 的“重启”按“保留 XDG data 目录”实现即可。

### 4.5 历史与分页（P22 PARTIAL）

- `/session/{id}/message` 按**时间正序**返回；`limit` 原生生效，语义是“最近 N 条”，结果仍正序。
- `before` 对合法 message ID 返回 **400**，legacy 分页不可用。
- 非法形状的 session ID（无 `ses_` 前缀）返回 **500**，属于 1.17.13 缺陷。
- **C04 影响**：不能用 `before` 做历史游标；必须在 seam/DTO 层自建游标（基于 message 时间或 ID），并对 session ID 形状做入参校验，统一返回 400/404。

### 4.6 provider 失败（P19 PARTIAL）

- provider 500/断连时 OpenCode 进入**无限重试**，不产生 `session.error`、不自动终止。
- **R04 影响**：超时、熔断、最大重试必须由调用方实现，不能依赖 OpenCode 的失败信号。

### 4.7 非法 model 污染会话（高风险）

- 非法 model 返回 204 + `session.error: Model not found`，不调用 provider，但**非法 model 会写进 session**；之后省略 model 的请求继续失败，需显式合法 model 才能恢复。
- **C04/M2 影响**：应用层必须在写入会话前校验 model（对照 provider 目录），非法时返回 404，避免污染会话状态。

## 5. 风险与缺陷清单

| 编号 | 事实 | 级别 | 归属 |
| --- | --- | --- | --- |
| R-01 | 非法 model 写进 session，后续省略 model 持续失败 | 高 | C04/M2：应用前校验并返回 404 |
| R-02 | provider 500/断连无限 retry，无终止错误 | 高 | R04：自建超时/熔断 |
| R-03 | `before` 分页不可用（400） | 中 | C04：自建游标 |
| R-04 | 非法形状 session ID 返回 500 | 中 | C04：入口校验，统一 400/404 |
| R-05 | prompt 带错 directory 返回 204，而非拒绝 | 低 | 契约记录即可 |
| R-06 | `tools` 显式 map 是整体覆盖，不是逐 key 合并 | 中 | C04：每轮下发完整 map |
| R-07 | legacy messageID 不能严格幂等（需 watchdog） | 中 | C04：不基于 legacy messageID 做幂等 |

## 6. 证据快照与重复性

- 版本：`1.17.13`（`opencode-ai`）
- 命令：

```bash
# 核心：L1 22 + L2 required 4
scripts/probe/run.sh -q

# L2 optional 4
pytest tests/probes/test_client_smoke.py -m "probe and client_smoke and optional" -q

# L3 真实 LLM（key 只从环境变量读）
WITTY_PROBE_REAL_PROVIDER=deepseek \
WITTY_PROBE_REAL_MODEL=deepseek-flash \
WITTY_PROBE_REAL_API_KEY=<env> \
pytest tests/probes/test_real_llm_smoke.py -m real_llm -q
```

- 核心套件连续 3 次（2026-11-10）：

| 次 | 结果 | 耗时 |
| --- | --- | --- |
| 1 | `25 passed, 6 deselected, 1 xfailed` | 121.25s |
| 2 | `25 passed, 6 deselected, 1 xfailed` | 113.17s |
| 3 | `25 passed, 6 deselected, 1 xfailed` | 108.41s |

- L2 optional：`4 passed, 4 deselected`（20.69s）。
- L3 真实 LLM：`2 passed`（13.77s，DeepSeek `deepseek-flash`）。
- `xfailed` 固定为 P19（PARTIAL），不是随机失败。

## 7. 未覆盖 / 后续

- `/api`（进程内 `/api/**`）spike：已执行，结论见 `opencode-1-17-13-api.md`，不属于 legacy 套件范围。
- `permission` / `question` 交互与拒绝路径。
- `format`、`variant`、`noReply`、多 Part（file/agent/subtask）输入。
- 并发 Turn、同一 Session 排队与取消竞态、事件重连。
- 多 Workspace / 多实例的事件与状态隔离。
- revert 与 git diff/snapshot 的联动。
- 1.17.13 → 新版本升级时的差异重跑（升级时必须重新执行本矩阵）。
