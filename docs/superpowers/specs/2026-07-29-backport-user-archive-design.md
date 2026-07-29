# Backport 用户归档精简设计

日期：2026-07-29

状态：已批准设计，待制定实施计划

## 1. 背景

当前 Backport 归档按 execution、case、attempt、patches 等内部执行概念分层，同时保存缓存、标准输出、重复报告、重复 patch 和完整分支列表。一个任务可能产生数千个文件，普通用户难以判断：

- 这是哪一次 Excel 导入产生的任务；
- 一键运行、暂停、继续和重新运行之间是什么关系；
- 某个 case 最终成功还是失败；
- 应该查看哪份 patch、日志或冲突报告；
- 哪些文件是结果，哪些只是内部临时产物。

本设计将归档定位为“普通用户可以直接查看和理解的运行记录”，而不是完整的开发调试快照。开发调试所需的临时缓存和原始子进程输出不进入长期归档。

## 2. 目标

新结构必须满足以下目标：

1. 每次导入 Excel 都形成一个边界清晰、名称可识别的新 Task。
2. 一次“一键运行”形成一个 Run；暂停和继续不会人为拆成多个 Run。
3. 单个 case 的最终结果和必要证据可以从一个稳定目录中找到。
4. 保留恢复运行、解释失败和查看最终结果所需的最小输入输出。
5. 不保留缓存、空日志、原始 stdout、完整分支枚举和重复文件。
6. 前端可以通过受控 API 展示历史、Run、case 和产物，不依赖任意服务器文件浏览。
7. 写入中断时不产生半写 JSON；服务重启后可以识别和恢复未结束的 Run。

## 3. 非目标

本次设计不承担以下能力：

- 保存 cvekit、Joern 或其他工具的全部调试现场；
- 从新结构兼容读取或迁移旧版 Backport 归档；
- 根据 Excel 内容哈希自动合并或复用历史 Task；
- 允许前端读取 Task 目录以外或未列入白名单的服务器文件；
- 在目标仓库已变化或工作区不干净时强行恢复运行；
- 将 API Key、访问令牌或其他凭证写入归档。

## 4. 核心概念

### 4.1 Task

一次“导入 Excel 并生成报告”创建一个全新 Task。

- 即使 Excel 文件名和目标仓库与上一次相同，也创建新 Task。
- 一个 Task 只对应一份导入 Excel、一份精简执行配置和一份初始报告。
- Task 是历史列表中的顶层记录。

### 4.2 Run

用户在一个 Task 中点击“一键运行”创建一个 Run。

- 第一次点击创建 Run 001。
- 暂停、服务中断后继续，仍更新 Run 001。
- 恢复时增加 `resume_count`，并记录 `paused`、`interrupted`、`resumed` 等事件。
- 只有在前一个 Run 已结束后，用户明确再次点击“一键运行”，才创建 Run 002。
- Run 结束后，其 JSON 和报告快照视为不可再修改的历史记录。

### 4.3 Case

Case 对应 Excel 中的一条 Backport 记录。

- Case 目录跨 Run 稳定复用。
- 每个 Run 在自己的 JSON 中引用参与处理的 case。
- Run 特有的 patch、日志和报告通过 `run-NNN-` 前缀保存在 case 目录。
- 单个 case 失败不会终止整批任务；系统继续处理其他 case。

## 5. 归档根目录

归档根目录必须由运行环境解析：

```text
${WITTY_WORKSPACE_ROOT}/backport-runs/
```

不得在代码、API 返回或前端文案中硬编码 `/home/dev/witty-workspace`。如果未显式配置环境变量，继续使用 witty-service 已有的 workspace 根目录默认值。

所有写入、读取、清理和路径校验均以解析后的归档根目录为边界。

## 6. 目录结构

```text
${WITTY_WORKSPACE_ROOT}/backport-runs/
└─ 20260729-commit-527-head5-kernel-a1b2c3/
   ├─ task.json
   ├─ input/
   │  ├─ source.xlsx
   │  ├─ config.json
   │  ├─ initial-report.yml
   │  └─ cvekit.log
   ├─ runs/
   │  ├─ 001.json
   │  ├─ 001-report.yml
   │  ├─ 002.json
   │  └─ 002-report.yml
   └─ cases/
      └─ 003-569155d2912d/
         ├─ case.json
         ├─ original.patch
         ├─ run-001-resolved.patch
         ├─ run-001-cvekit.log
         ├─ run-001-resolution.log
         └─ run-001-conflict-report.json
```

不再创建以下长期归档层级：

```text
executions/
attempts/
steps/
patches/
cache/
logs/
```

`input/cvekit.log`、Run 报告和 case 产物均按实际需要创建；空文件不创建。

## 7. 命名规则

### 7.1 Task 目录

格式：

```text
YYYYMMDD-<excel-stem>-<target-repo>-<random-id>
```

示例：

```text
20260729-commit-527-head5-kernel-a1b2c3
```

规则：

- 日期取 Task 创建日期。
- `excel-stem` 来自 Excel 文件名，不包含扩展名。
- `target-repo` 取目标仓库 URL 或本地路径的最后一个有效名称，并移除 `.git`。
- 两个名称均转为适合文件系统的短 slug，仅保留小写字母、数字和连字符。
- 连续分隔符合并，首尾分隔符移除。
- 两个 slug 分别限制为 40 个字符。
- slug 结果为空时分别使用 `excel` 和 `repository` 兜底。
- `random-id` 使用 6 位小写十六进制随机值，避免同日同名任务碰撞。
- 若目录仍发生碰撞，重新生成 `random-id`，不得复用已有 Task。
- 完整 Excel 文件名和仓库 URL 只保存在 `task.json`。

### 7.2 Run 文件

Run 编号为 Task 内从 1 开始的三位十进制序号：

```text
001.json
001-report.yml
002.json
002-report.yml
```

编号只在用户明确创建新 Run 时递增。

### 7.3 Case 目录

格式：

```text
<Excel 行序号三位数>-<commit 前 12 位>
```

示例：

```text
003-569155d2912d
```

这里的行序号是导入后业务表格显示的稳定序号，不使用工作表物理行号。完整 commit、标题和源数据标识保存在 `case.json`。

## 8. 文件定义

### 8.1 `task.json`

`task.json` 是 Task 的小型索引，不复制完整 Run、case、报告或事件内容。

示例：

```json
{
  "schema_version": 3,
  "task_id": "20260729-commit-527-head5-kernel-a1b2c3",
  "name": "commit_527_head5.xlsx",
  "status": "completed_with_failures",
  "current_run": 1,
  "current_report": "runs/001-report.yml",
  "summary": {
    "total": 5,
    "success": 4,
    "failed": 1
  },
  "target": {
    "repository": "https://example.com/kernel.git",
    "branch": "devel-6.6",
    "head": "d0e1a36154c8"
  },
  "created_at": "2026-07-29T07:30:00Z",
  "updated_at": "2026-07-29T08:15:00Z"
}
```

约束：

- `schema_version` 固定用于新结构版本识别。
- `status` 使用第 9 节定义的 Task 状态。
- `current_run` 在尚未执行一键运行时为 `null`。
- `current_report` 在初始报告生成成功后指向 `input/initial-report.yml`，Run 创建后指向该 Run 的报告；报告生成失败时为 `null`。
- `summary` 只保存当前用户界面需要的计数。
- `target.head` 更新为最近一个完成或暂停快照的目标 HEAD。
- 不保存分支枚举、完整报告、case 数组、事件数组、API Key 或命令行环境。

### 8.2 `input/source.xlsx`

保留用户导入的 Excel 原文件，统一命名为 `source.xlsx`。原始文件名保存在 `task.json.name`。

每个 Task 保留自己的 Excel，因此旧 Run 的输入可以完整还原，不需要计算 Excel 内容哈希或建立 Excel 版本目录。

### 8.3 `input/config.json`

只保存复现和解释本 Task 所必需的输入：

- 源仓库 URL、选中的源分支和必要本地路径；
- 目标仓库 URL、选中的目标分支和必要本地路径；
- 目标目录布局或版本映射；
- Backport 模型记录 ID、显示名称和非敏感模型参数；
- signer 名称、邮箱、commit message 模板；
- 实际使用的非敏感 cvekit 选项。

明确不保存：

- 本地或远端全部分支列表；
- 页面 capabilities、warnings 等展示快照；
- cache 目录；
- 当前 report 的绝对路径；
- API Key、Token、密码或其他凭证；
- 可从其他字段推导出的重复值。

### 8.4 `input/initial-report.yml`

保存导入 Excel 并生成报告后的初始业务报告，作为所有 Run 开始前的基线。只有初始报告成功生成后才创建该文件。

不再同时保存 `backport.base.yml`、`backport-batch.yml`、多个 root/current/latest 镜像或同内容的 report 副本。若 cvekit 内部仍需要这些工作文件，应放在临时工作目录，完成后只提取 `initial-report.yml`。

### 8.5 `input/cvekit.log`

只在生成初始报告期间 cvekit 的 stderr 非空且对用户有诊断价值时创建。

- 不保存 stdout。
- 写入前执行凭证脱敏。
- 纯进度噪声可以过滤。
- 空白或过滤后为空的日志不创建。

### 8.6 `runs/NNN.json`

Run JSON 是一次一键运行的状态和结果摘要。

示例：

```json
{
  "run": 1,
  "status": "completed_with_failures",
  "resume_count": 1,
  "started_at": "2026-07-29T07:40:00Z",
  "finished_at": "2026-07-29T08:15:00Z",
  "target_start": {
    "repository": "https://example.com/kernel.git",
    "branch": "devel-6.6",
    "head": "abc123456789",
    "clean": true
  },
  "target_end": {
    "repository": "https://example.com/kernel.git",
    "branch": "devel-6.6",
    "head": "d0e1a36154c8",
    "clean": true
  },
  "summary": {
    "processed": 5,
    "success": 4,
    "failed": 1
  },
  "cases": [
    {
      "id": "003-569155d2912d",
      "status": "failed",
      "actions": ["check", "resolve"],
      "message": "自动解冲突失败"
    }
  ],
  "events": [
    {
      "type": "paused",
      "at": "2026-07-29T07:55:00Z"
    },
    {
      "type": "resumed",
      "at": "2026-07-29T08:00:00Z"
    }
  ],
  "report": "001-report.yml"
}
```

约束：

- `status` 使用第 9 节定义的状态。
- `finished_at` 仅在终态出现。
- `target_start` 在 Run 创建时固定。
- `target_end` 在暂停、中断和终态快照时更新。
- `cases` 每个 case 只保存用户可见摘要，不嵌入完整日志、patch 或报告。
- `events` 只保存生命周期事件，不记录每条内部进度消息。
- 暂停和恢复原子更新同一个 JSON。
- Run 到达终态后不得再被恢复或覆盖；重新执行必须创建下一编号。

### 8.7 `runs/NNN-report.yml`

保存该 Run 最近一次可恢复或终态业务报告：

- Run 创建时从初始报告生成。
- 执行中按能够安全恢复的检查点更新。
- 暂停、服务中断或系统异常时尽最大可能刷新。
- Run 到达终态后冻结。

同一 Run 只保留这一份报告，不创建 `current`、`latest`、`filtered` 或 case 级报告副本。

### 8.8 `cases/<case-id>/case.json`

Case JSON 是跨 Run 的稳定索引。

示例：

```json
{
  "case_id": "003-569155d2912d",
  "row": 3,
  "commit": "569155d2912d6d110713c2dbf2a9bcf8fe5bb2",
  "title": "ACPI: PCC: example change",
  "status": "failed",
  "applied_commit": null,
  "last_run": 1,
  "artifacts": {
    "original_patch": "original.patch"
  },
  "runs": {
    "001": {
      "status": "failed",
      "applied_commit": null,
      "artifacts": {
        "cvekit_log": "run-001-cvekit.log",
        "resolution_log": "run-001-resolution.log",
        "conflict_report": "run-001-conflict-report.json"
      }
    }
  },
  "updated_at": "2026-07-29T08:10:00Z"
}
```

约束：

- `status`、`applied_commit` 和 `last_run` 反映最近处理结果。
- 顶层 `artifacts` 只保存跨 Run 共享的 `original_patch`。
- `runs` 按 Run 编号索引该次结果和产物，确保后续 Run 不会覆盖或隐藏历史证据。
- 每个 `artifacts` 对象只列出实际存在的文件。
- Run 历史结果以对应 `runs/NNN.json` 为准，不能只依赖 case 当前状态。
- 原始 patch 内容相同则只保留一份 `original.patch`。

### 8.9 Case 产物

允许的产物名称：

```text
original.patch
run-NNN-resolved.patch
run-NNN-cvekit.log
run-NNN-resolution.log
run-NNN-conflict-report.json
```

保留规则：

- `original.patch`：保留导入记录对应的原始 patch。
- `run-NNN-resolved.patch`：仅当解冲突后 patch 与 `original.patch` 内容不同时保留。
- `run-NNN-cvekit.log`：仅保留该 Run 对该 case 有诊断价值、非空且已脱敏的 cvekit stderr。
- `run-NNN-resolution.log`：保留 Backport/Mystique 解冲突过程的用户可读日志。
- `run-NNN-conflict-report.json`：保留最终 OpenCode conflict report。

若同一 case 在 Run 001 解冲突成功、Run 002 可以直接应用：

- `run-001-resolved.patch` 和 Run 001 的解冲突证据继续保留；
- Run 002 在 `002.json` 中记录 `actions: ["check", "apply"]`；
- Run 002 没有新的解冲突产物时，不创建空的 `run-002-resolution.log` 或重复 patch。

## 9. 状态模型

### 9.1 Task 状态

允许状态：

```text
generating
ready
generation_failed
pending
running
paused
interrupted
completed
completed_with_failures
failed
```

含义：

- `generating`：已创建 Task，正在根据 Excel 生成初始报告。
- `ready`：初始报告生成成功，尚未创建 Run。
- `generation_failed`：初始报告生成失败；保留 Excel、精简配置和有价值的 cvekit 日志，但不允许创建 Run。
- 创建 Run 后，Task 跟随当前 Run 的 `pending`、`running`、`paused`、`interrupted` 或终态。

生成失败后重新导入 Excel 会创建新 Task，不复用或覆盖失败 Task。

### 9.2 Run 状态

允许状态：

```text
pending
running
paused
interrupted
completed
completed_with_failures
failed
```

含义：

- `pending`：Run 已创建但尚未进入执行循环。
- `running`：正在处理。
- `paused`：用户主动暂停，可继续同一 Run。
- `interrupted`：服务或进程非正常中断，可在校验通过后继续同一 Run。
- `completed`：全部 case 成功或无需处理。
- `completed_with_failures`：批次正常跑完，但至少一个 case 业务失败。
- `failed`：批次因系统级异常无法继续或完成。

终态为：

```text
completed
completed_with_failures
failed
```

### 9.3 Case 状态

至少支持：

```text
pending
running
applied
resolved
skipped
failed
```

具体页面可以将 `applied`、`resolved` 映射为“成功”，但归档保留实际结果。

### 9.4 失败策略

- Git 冲突无法自动解决属于 case 业务失败。
- 该 case 标记为 `failed`，保存 cvekit 日志、解冲突日志和 OpenCode conflict report。
- 执行循环继续处理后续 case。
- 批次结束时，只要存在业务失败，Run 和 Task 状态为 `completed_with_failures`。
- 无法访问仓库、归档写入失败、不可恢复的子进程异常等系统问题使 Run 状态为 `failed`。

## 10. Run 生命周期

```text
创建 Run 001
  -> pending
  -> running
     -> 用户暂停 -> paused -> 继续 -> running
     -> 服务中断 -> interrupted -> 恢复 -> running
     -> 全部处理完成 -> completed / completed_with_failures
     -> 系统异常无法继续 -> failed

终态后用户再次点击一键运行
  -> 创建 Run 002
```

暂停或恢复时：

1. 更新 Run 报告快照；
2. 更新目标仓库快照；
3. 原子写入 Run JSON；
4. 同步更新 `task.json`；
5. 恢复时增加 `resume_count` 并追加生命周期事件。

不再使用额外 `activity.log`；Run JSON 中的 `events` 已覆盖必要生命周期信息。

## 11. 目标仓库变化与恢复

Run 创建时记录目标仓库起始快照，暂停、中断和结束时记录当前快照。

恢复同一 Run 前必须校验：

1. 目标仓库本地路径仍存在并且是 Git 仓库；
2. 配置的 remote URL 与 Run 记录一致；
3. 当前分支与 Run 预期分支一致；
4. 工作区处于预期的干净状态；
5. 当前 HEAD 与暂停或中断时保存的 `target_end.head` 一致。

任一校验不通过时：

- 不自动修改、reset 或清理用户仓库；
- 不继续执行；
- 返回清晰的阻塞原因和预期/实际值；
- Run 保持 `paused` 或 `interrupted`，等待用户处理。

用户如果希望基于已经变化的仓库重新执行，应显式创建新 Run；新 Run 记录新的 `target_start`。

创建新 Run 前同样要求目标仓库存在、分支正确且工作区干净，但允许 HEAD 与上一 Run 的结束值不同，因为这正是新 Run 的新起点。

服务启动时应扫描 Task 中仍标记为 `running` 的 Run：

- 将其原子更新为 `interrupted`；
- 追加一次 `interrupted` 事件，事件需具备幂等标识，重复启动不得重复追加；
- 保留最后一份完整的 Run 报告和目标仓库快照；
- 等待用户显式恢复，不自动继续执行。

## 12. 临时工作目录

cvekit、Joern 和其他工具产生的下列内容放入 Task 目录之外的独立临时工作目录：

- CPG、PDG 和分析缓存；
- 中间 YAML、filtered report；
- 原始 stdout；
- 命令描述文件；
- case attempt/result 中间 JSON；
- 内容重复的 patch 副本。

处理流程：

1. 为一次命令或 case 创建受控临时目录；
2. 工具在临时目录中执行；
3. 提取白名单中的最终报告、差异 patch 和必要日志；
4. 对日志脱敏；
5. 原子写入 Task 归档；
6. 命令结束后清理临时目录。

即使工具失败，也只提取用户需要的诊断证据，不把整个临时目录移动进归档。

## 13. 原子写入和并发

- JSON、YAML 报告和文本日志均先写同目录临时文件，再通过原子替换发布。
- Task 内 Run 编号分配和索引更新必须使用现有进程内锁。
- 创建 Task 目录时使用带随机后缀的唯一名称，并验证目录不存在。
- `task.json` 和 Run JSON 的字段更新由单一 Store 接口负责，避免多个业务路径各自拼接结构。
- 归档写入失败不得被当作 case 业务失败静默忽略。
- 读取时拒绝解析不在归档根目录内的路径或符号链接逃逸。

## 14. 日志脱敏

所有长期日志写入前至少屏蔽：

- `API_KEY`、`OPENAI_KEY` 和常见 Token 环境变量；
- HTTP `Authorization` 请求头；
- URL 查询参数中的 token、key、secret；
- 命令行中的 `--api-key` 及其值；
- 已知模型和 MCP 配置中的凭证值。

脱敏后的值统一显示为：

```text
[REDACTED]
```

归档 Store 只接收已脱敏文本，或在唯一入口执行统一脱敏，避免各调用方遗漏。

## 15. API 设计

API 层明确区分 Task 和 Run，具体路由命名可在实施时保持现有公共前缀兼容，但语义必须如下。

### 15.1 Task

- 导入 Excel 并生成报告：立即创建新 Task；成功转为 `ready`，失败转为 `generation_failed`。
- 列出 Task：返回历史列表所需摘要。
- 获取 Task：返回 `task.json`、Run 摘要和 case 摘要。
- 获取 Task 初始报告：返回白名单文件 `input/initial-report.yml`。

### 15.2 Run

- 创建 Run：仅在当前没有非终态 Run 时允许，编号递增。
- 暂停 Run：只允许暂停 `running`。
- 恢复 Run：只允许恢复 `paused` 或 `interrupted`，不创建新编号。
- 获取 Run：返回 Run JSON。
- 获取 Run 报告：返回 `runs/NNN-report.yml`。

重复恢复请求应幂等：已恢复的 Run 不增加第二次 `resume_count`，也不重复启动执行循环。

### 15.3 Case 和产物

- 获取 case：返回 `case.json` 及按 Run 分组的摘要。
- 获取 case 产物：只能读取 `case.json` 顶层或 `runs` 中登记的 artifact，且文件名必须符合服务端白名单。
- 支持浏览器内查看文本、复制归档路径和下载文件。
- 拒绝绝对路径、`..`、符号链接逃逸和未登记文件。

## 16. 前端展示

### 16.1 历史列表

每个 Task 显示：

- Excel 原文件名；
- 目标仓库短名称和分支；
- Task 状态；
- 成功/失败数量；
- 最近更新时间。

Task 目录 ID 可作为次要信息展示或复制，不作为主要标题。

### 16.2 Task 详情

按三层信息组织，但不映射为三层磁盘目录：

1. Task 概览：输入、目标仓库、初始报告、总体状态；
2. Run 列表：Run 编号、状态、恢复次数、开始/结束时间和汇总；
3. Case 列表：行号、commit、标题、最近结果。

### 16.3 Case 详情

按 Run 展示：

- 本次动作，例如 check、resolve、apply；
- 原始 patch 和实际不同的 resolved patch；
- cvekit stderr；
- Backport/Mystique 解冲突日志；
- OpenCode conflict report；
- 应用后的 commit。

缺失的可选产物不显示空入口。

## 17. 旧归档处理

不实现旧结构迁移或兼容读取。用户将重新导入 Excel 创建新 Task。

实施清理前必须：

1. 解析并显示当前 `${WITTY_WORKSPACE_ROOT}`；
2. 确认实际 `backport-runs` 根目录位于允许范围内；
3. 列出准备删除的旧 Task 目录；
4. 不删除配置、目标仓库、Excel 原文件或根目录之外的内容；
5. 再执行已获授权的旧 Task 清理。

清理完成后，新 Task 只使用本规格定义的新结构。

## 18. 与当前概念的映射

| 当前概念或文件 | 新设计 |
| --- | --- |
| 顶层 run-id 目录 | Task 目录 |
| `executions/NNN` | 删除；显式一键运行改为 `runs/NNN.json` |
| pause/resume 后的新 execution | 删除；继续更新同一个 Run |
| `attempts/NNN` | 删除 |
| case `result.json` | 合并为 `case.json` 和 Run 中的 case 摘要 |
| `command.json` | 不归档 |
| `stdout.log` | 不归档 |
| `stderr.log` | 过滤、脱敏后按用途命名，非空才归档 |
| 多份 original/current/backported patch | 内容去重后保留 `original.patch` 和真正不同的 resolved patch |
| root/current/latest/filtered 报告镜像 | 初始报告一份，每个 Run 报告一份 |
| 完整 source/target branch 数组 | 不归档，只保存选中分支 |
| Joern/cvekit cache | Task 外临时使用，结束后删除 |

## 19. 验收标准

### 19.1 Task 和 Run 行为

- 每次导入 Excel 都创建不同 Task，即使文件名和目标仓库相同。
- Task 名称包含日期、Excel 名、目标仓库名和唯一短 ID。
- 初始报告生成失败时 Task 为 `generation_failed`，不产生空报告且不能创建 Run。
- 第一次一键运行创建 Run 001。
- 暂停后继续仍为 Run 001，且 `resume_count` 增加一次。
- 服务重启恢复仍为原 Run。
- 终态后明确重新运行才创建 Run 002。
- 非终态 Run 存在时不得并发创建下一 Run。

### 19.2 Case 和失败行为

- Case 目录使用三位表格序号和 12 位 commit。
- 单个 case 解冲突失败会保存必要证据并继续后续 case。
- 存在 case 失败且批次完成时，Run/Task 为 `completed_with_failures`。
- 同一 case 后续 Run 直接应用时，历史解冲突产物不丢失，也不生成空产物。
- `case.json.runs` 可以直接定位每个 Run 的 case 产物。

### 19.3 文件精简

新 Task 中不得出现：

- `cache/`、`executions/`、`attempts/`、`steps/`；
- `stdout.log`、`command.json`、attempt/result 中间 JSON；
- 完整本地或远端分支列表；
- 内容相同的 patch 副本；
- root/current/latest/filtered 报告副本；
- 空日志。

### 19.4 安全和恢复

- 日志中的已知凭证被替换为 `[REDACTED]`。
- 任意路径穿越和符号链接逃逸请求被拒绝。
- JSON 和报告在模拟中断后保持上一份完整内容。
- 服务启动会将遗留的 `running` Run 幂等收敛为 `interrupted`。
- 目标 remote、branch、HEAD 或 clean 状态不一致时，恢复被阻止且不修改仓库。
- 归档根目录始终从 workspace 配置解析。

### 19.5 体积目标

不以固定文件大小作为正确性条件，但一个普通 Task 的长期体积应主要由 Excel、业务报告和真实 patch/log 决定，不再由 Joern/cvekit 缓存或重复文件主导。对当前典型样本，新结构应消除绝大多数文件数量和缓存占用。

## 20. 实施边界

后续实施计划应覆盖：

- `BackportRunStore` 新 schema 和原子读写；
- `BackportService` 的 Task/Run 生命周期；
- `BackportCvekitClient` 的临时目录、日志过滤和产物提取；
- Backport API schema 和路由语义；
- polymind 的 Task/Run/Case 历史展示与恢复动作；
- 单元测试、API 测试和前端类型检查；
- 旧归档的受控清理；
- 后端重启及一条全新 Excel 导入的端到端验证。

实现不得改变已批准的 Task、Run、Case 语义；若发现当前 cvekit 接口无法提供某项必要产物，应先回到设计层确认替代方案，而不是重新引入整份临时目录归档。
