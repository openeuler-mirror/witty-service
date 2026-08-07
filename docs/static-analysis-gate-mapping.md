# 静态代码检查与社区门禁对齐说明

> 更新日期：2026-08-07。本文档说明 witty-service 的静态检查工具选型、与 openEuler 社区门禁规则的映射关系、本地/CI 落地步骤，以及无开源工具覆盖时由社区门禁远端服务补齐的检查项。原则：**开源工具覆盖什么、门禁/AI 远端服务覆盖什么**，本地不重复实现。

## 1. 参考与背景

- 开源代码检查指南（openlibing/docs）：[static-code-analysis](https://gitcode.com/openlibing/docs/tree/main/static-code-analysis)
- pre-commit 落地指南：[pre-commit-solution.md](https://gitcode.com/openlibing/docs/blob/main/static-code-analysis/solutions/pre-commit-solution.md)
- Python 语言检查工具选型：[Python.md](https://gitcode.com/openlibing/docs/blob/main/static-code-analysis/languages/Python.md)
- 社区门禁代码：[openeuler-jenkins](https://atomgit.com/openeuler/openeuler-jenkins)（`src/ac`、`src/conf/.gitlint`）

落地方法：选开源工具 → 列出开源规则全集 → 匹配社区门禁规则 → 生成工具配置 → 无开源匹配的规则由门禁远端服务补齐。pre-commit 作为本地提交与 CI 门禁的**统一入口**（`.pre-commit-config.yaml`）：本地 `.githooks/pre-commit` 增量检查暂存文件，GitHub Actions `lint.yml` 与社区门禁复用同一份配置与同一批工具，避免两套命令漂移；mypy strict 作为独立类型检查（本地手动命令，不入 pre-commit），兜底 pylint `E0401` 之外的导入/类型类真实错误。

## 2. 工具选型（已落地）

| 工具 | 版本/位置 | 作用 |
| --- | --- | --- |
| pre-commit | ≥ 4.0，`.pre-commit-config.yaml` | 本地 + CI 统一调度入口；`.githooks/pre-commit` 增量检查暂存文件 |
| pre-commit-hooks | v6.0.0（GitCode 镜像） | 通用 hygiene：行尾空白、EOF、YAML/JSON/TOML 语法、合并冲突、大文件、私钥、调试语句 |
| Ruff | v0.15.17，`ruff-check`（`--fix`）+ `ruff-format` | lint + 格式化（替代 Black + Flake8），规则 `E,W,F,I,B,UP,SIM,RUF`；忽略 `E501,E203` 格式兼容项、`RUF001/2/3` 中文标点噪声、`B008/E402` 存量治理项 |
| Bandit | 1.9.4，`-c pyproject.toml` | Python 安全扫描（排除 `tests`、`data`；B404/B603/B607 为子进程核心能力集中跳过） |
| mypy | strict 模式（本地手动命令，不入 pre-commit） | 类型检查，兜底真实 import/类型错误 |
| Codespell | v2.4.2 | 拼写检查（默认词典 + 项目忽略词表） |
| ShellCheck | local hook，`--severity=warning`（本机安装时生效） | Shell 静态检查 |
| Markdownlint | v0.45.0，`--fix` | Markdown 检查（关闭 MD013/MD029/MD033/MD036/MD040，适配存量文档） |
| gitlint | v0.19.1，commit-msg 阶段 | commit message 规范（对齐门禁 check_commit_msg，保障 semantic-release 解析） |
| Gitleaks | v8.30.1（已注释） | 密钥扫描；Go 构建环境依赖 go.dev/GitHub，网络就绪后启用，当前由 `detect-private-key` 兜底 |

## 3. 社区门禁规则映射

| 社区门禁检查项 | 门禁规则要点 | 开源工具匹配 | 归属 |
| --- | --- | --- | --- |
| check_code_style | Python(pylint3 `--disable=E0401`，消息类型 C/R/W/E/F；F 致命、W/E 告警)/Go(golint)/C++(splint) | Ruff F/B/E9 对应致命/错误，W/E 对应警告，C/R 对应 SIM/UP/RUF + format；真实 import 错误由 mypy 兜底；本仓无 Go/C/C++ 文件不启用 | 开源匹配 |
| check_openlibing | 远端 AI codecheck（缺陷/安全/语义） | Ruff B + Bandit + mypy 覆盖规则子集 | 门禁远端补齐 |
| check_sca | ScanOSS 代码片段/许可证扫描 | 无开源等价 | 门禁远端补齐 |
| check_anti_poisoning | 供应链投毒检测 | 无开源等价 | 门禁远端补齐 |
| check_package_license | license 白名单/一致性 | 无本地等价（LICENSE 文件与 pyproject.toml 已声明 MIT） | 门禁远端补齐 |
| check_commit_msg | gitlint：title 5–72、`type: subject` 正则、body 5–80 | 本地 gitlint hook 已对齐（`.gitlint`）；不启用 DCO/强制正文（witty-ub 门禁未启用该检查，README 允许单行提交） | 开源匹配 |

## 4. 门禁远端检视要点（无本地开源工具覆盖）

以下检查项无本地开源等价，已由社区门禁远端服务自动补齐（`witty-ub.yaml` 启用 openlibing / anti_poisoning）。代码评审时按清单确认语义级问题：

1. **语义与正确性**：并发/竞态、资源泄漏、状态机转换、异常路径（对应 check_openlibing）
2. **安全**：越权/认证绕过、注入、敏感信息泄露（对应 check_openlibing）
3. **供应链投毒**：新增依赖来源与完整性（对应 check_anti_poisoning）
4. **代码片段**：重复/复制代码与第三方代码片段溯源（对应 check_sca）
5. **License 合规**：新增依赖的 license 与项目 MIT 的兼容性（对应 check_package_license）

## 5. 落地步骤

### 5.1 配置文件清单（本仓库已提交）

- `.pre-commit-config.yaml`：统一入口——pre-commit-hooks（hygiene/大文件/私钥/调试语句）+ Ruff（`--fix` + format）+ Bandit + Codespell + ShellCheck + Markdownlint + gitlint（commit-msg 阶段）；Gitleaks 已注释待网络就绪
- `pyproject.toml`：`[tool.ruff]`（select `E,W,F,I,B,UP,SIM,RUF`）、`[tool.bandit]`、`[tool.codespell]`、`[tool.mypy]`（strict）
- `.markdownlint.jsonc`：关闭 MD013/MD029/MD033/MD036/MD040
- `.gitlint`：标题 5–72、`type(scope): subject` 正则、正文 5–80，与门禁参数对齐
- `.githooks/pre-commit`：本地钩子，`pre-commit run` 增量检查暂存文件

### 5.2 接入本地

```bash
uv tool install pre-commit
pre-commit install --hook-type pre-commit --hook-type commit-msg   # 注册 hooks
# 或沿用仓库习惯：
cp .githooks/pre-commit .git/hooks/pre-commit
pre-commit install --hook-type commit-msg
pre-commit run --all-files   # 全量检查（默认仅检查暂存文件）
```

### 5.3 接入 PR（CI）

- GitHub Actions `lint.yml`：Python job 以 `pre-commit run --all-files` 全量检查（前端 job 独立 npm lint/tsc），与本地复用同一批 hook 工具，保证本地与 CI 口径一致
- 社区门禁：`witty-ub.yaml` 已启用 openlibing / anti_poisoning，与本地开源工具互补，SCA / license 白名单等由远端服务补齐

## 6. 历史问题治理策略

- 新代码：直接满足严格规则（错误级）
- 存量项目（本仓库）：不立即阻断历史问题——
  - 本地提交 pre-commit 只检查暂存文件，PR 只检查变更文件，历史文件不动
  - 存量告警按表分批治理，每批独立提交，修复后从 `ignore` 移除并收紧规则

| 规则 | 当前存量 | 处置 |
| --- | --- | --- |
| RUF001/RUF002/RUF003 | 598 | 中文全角标点误报，长期忽略 |
| B008 | 81 | 默认参数函数调用，列入治理队列，修复后从 ignore 移除 |
| E402 | 13 | 模块级代码后的导入，列入治理队列 |
| B904/B006/SIM\*/UP\*/RUF0\*/F841/F821/E731/B024 | ~80 | 已在本轮修复 |

- 新规则先增量灰度：报告模式观察 → 稳定后收紧为错误级

## 7. 维护约定与屏蔽纪律

- 职责边界：本地 pre-commit 只覆盖可被开源工具精确表达的规则；SCA、anti-poisoning、语义级 AI 检视、license 白名单由社区门禁远端服务负责，不重复实现
- 告警处理顺序：先修代码 → 收敛规则配置 → 最小范围行级注释（带规则 ID 和原因）
- 工具/hook 版本升级单独提交，升级后全量运行 `pre-commit run --all-files` 确认影响范围
- 每季度清理一次屏蔽项与忽略词表
- 本地钩子统一由 pre-commit 管理；从旧版 hook 切换时先卸载旧钩子，避免与 `.git/hooks` 冲突

## 8. 国内网络注意事项

- pre-commit hook 仓库统一使用 GitCode 镜像（`gitcode.com/gh_mirrors/*`），避免 GitHub 克隆超时；pre-commit 本体需 pip/uv 安装（`uv tool install pre-commit`）
- Gitleaks 的 golang hook 安装时会下载 Go 工具链（go.dev），国内环境若不可达暂不启用，由 `detect-private-key` 兜底；网络就绪后预装系统 Go（如 `yum install golang`）并设置 `GOPROXY=https://goproxy.cn,direct`，pre-commit 会自动改用系统 Go
