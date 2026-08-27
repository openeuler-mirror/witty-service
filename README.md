# Witty-Service

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) [![PyPI version](https://img.shields.io/pypi/v/witty-service.svg)](https://pypi.org/project/witty-service/) [![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)

AI Agent 全生命周期管理服务。Witty-Service 提供智能体的创建、沙箱运行、会话管理、消息交互等核心能力，通过统一的 REST API 屏蔽底层沙箱（Docker / Local Process / E2B）与运行时适配器（OpenClaw / OpenCode）的差异，让你以一致的方式编排和管理 AI Agent。

## 目录

- [Witty-Service](#witty-service)
  - [目录](#目录)
  - [项目介绍](#项目介绍)
    - [功能特性](#功能特性)
    - [技术架构](#技术架构)
    - [整体框架图](#整体框架图)
  - [安装指南](#安装指南)
    - [方式一：pip安装](#方式一pip安装)
    - [方式二：从源码安装](#方式二从源码安装)
  - [快速开始](#快速开始)
  - [配置说明](#配置说明)
    - [核心配置](#核心配置)
    - [Docker 沙箱配置](#docker-沙箱配置)
  - [部署流程](#部署流程)
    - [测试环境](#测试环境)
    - [生产环境](#生产环境)
    - [启动参数说明](#启动参数说明)
    - [生产环境注意事项](#生产环境注意事项)
  - [本地开发](#本地开发)
    - [环境搭建](#环境搭建)
    - [项目结构](#项目结构)
    - [常用开发命令](#常用开发命令)
    - [代码贡献流程](#代码贡献流程)
    - [开发规范](#开发规范)
  - [许可证](#许可证)
  - [支持与反馈](#支持与反馈)

---

## 项目介绍

Witty-Service 是一个面向 AI Agent 场景的后端服务，核心职责是将 **Agent 生命周期管理**、**沙箱隔离运行**、**会话与消息交互** 三者统一封装，对外提供简洁的 RESTful API。它作为上层应用（如 PolyMind）与底层 Agent 运行时之间的桥梁，使得前端无需关心 Agent 是运行在 Docker 容器、本地进程还是云端沙箱中。

典型应用场景：

- **AI 编程助手平台** — 为每个用户创建独立沙箱中的 Agent，隔离执行代码生成、文件操作等任务
- **多模型对话服务** — 统一管理 OpenAI、Anthropic、DeepSeek 等多家 LLM 提供商的模型配置，按需切换
- **Agent 技能市场** — 通过技能仓库为 Agent 动态注入专业能力（如 CVE 分析等）
- **企业级 Agent 编排** — 支持定时任务、会话暂停/恢复、运行时备份等企业级运维能力

### 功能特性

- 🤖 **Agent 全生命周期管理** — 创建、暂停、恢复、删除 Agent，支持运行时备份与恢复
- 📦 **多沙箱隔离** — 支持 Docker、Local Process、E2B 三种沙箱类型，按需选择隔离级别
- 💬 **会话与消息** — 多会话管理，支持 REST 非流式和 SSE 流式两种消息交互模式
- 🔌 **多运行时适配** — 通过 Adapter 层对接 OpenClaw、OpenCode 等不同 Agent 运行时
- 🧠 **多模型管理** — 统一配置 OpenAI、Anthropic、Google、DeepSeek、GLM、Kimi 等 10+ 模型提供商
- 🛠️ **技能市场** — 内置技能仓库同步，支持从市场安装和上传自定义技能包
- 🔐 **安全认证** — 基于 Bearer Token 的 API 认证机制
- 📊 **CVE 与 Backport** — 内置 CVE 漏洞分析和Backport服务

### 技术架构

| 层级 | 技术栈 |
|------|--------|
| Web 框架 | FastAPI |
| ASGI 服务器 | Uvicorn |
| 数据库 | SQLAlchemy + Alembic（迁移） |
| 通信协议 | WebSocket + REST + SSE |
| 沙箱管理 | Docker SDK / subprocess / E2B SDK |
| 包管理 | uv |
| 语言 | Python 3.11+ |

### 整体框架图

```
                              ┌─────────────────────────────────┐
                              │      上层应用（如 PolyMind）      │
                              └───────────────┬─────────────────┘
                                              │
                                     REST API / SSE
                                              │
                                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│                           Witty-Service                              │
│                                                                      │
│  ┌─ API Layer ───────────────────────────────────────────────────┐   │
│  │  /agents  ·  /models  ·  /skills  ·  /cve  ·  /backport       │   │
│  │  Auth (Bearer Token)  ·  Error Handler  ·  Schemas            │   │
│  └──────────────────────────┬────────────────────────────────────┘   │
│                             │                                        │
│  ┌─ Application Layer ──────▼────────────────────────────────────┐   │
│  │  AgentManager · SessionManager · SkillManager                 │   │
│  │  CVEService  · BackportService                                │   │
│  └──┬────────────────────┬──────────────────────┬────────────────┘   │
│     │                    │                      │                    │
│  ┌──▼─────────────┐  ┌──▼──────────────┐  ┌───▼──────────────┐       │
│  │ Adapter Layer  │  │Persistence Layer│  │  Storage Layer   │       │
│  │ WebSocket客户端│  │ SQLAlchemy ORM  │  │  WorkspaceStore  │       │
│  │ HTTP 客户端    │  │ SQLite+Alembic  │  │  RuntimeBackup   │       │
│  │ 连接池/协议     │  │ Repository      │  │                  │       │
│  └──┬─────────────┘  └─────────────────┘  └──────────────────┘       │
│     │                                                                │
│  ┌──▼─────────────────────────────────────────────────────────────┐  │
│  │  Sandbox Layer                                                 │  │
│  │  ┌──────────┐  ┌──────────────┐  ┌──────────┐                  │  │
│  │  │  Docker  │  │Local Process │  │   E2B    │                  │  │
│  │  └────┬─────┘  └──────┬───────┘  └────┬─────┘                  │  │
│  │       └────────┬──────┘               │                        │  │
│  │                │                      │                        │  │
│  │       AdapterEndpoint                 │                        │  │
│  └────────────────┼──────────────────────┼────────────────────────┘  │
│                   │                      │                           │
│         ┌─────────────────────┐  ┌───────▼──────────┐                │
│         │ Domain (Enums/Errors)│  │   Config         │               │
│         └─────────────────────┘  └──────────────────┘               │
└──────────────────┼──────────────────────┼───────────────────────────┘
                   │                      │
         ┌─────────▼──────────┐  ┌────────▼─────────┐
         │   HTTP REST        │  │   HTTP REST       │
         │ (生命周期/技能管理)  │  │ (E2B Cloud API)  │
         └─────────┬──────────┘  └──────────────────┘
                   │
         ┌─────────▼──────────┐
         │   WebSocket        │
         │ (流式消息/事件推送)  │
         └─────────┬──────────┘
                   │
                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       Witty-Agent-Server                             │
│                                                                      │
│  ┌─ API Layer ───────────────────────────────────────────────────┐   │
│  │  AgentRouter  ·  SessionRouter  ·  SessionWSRouter            │   │
│  └──────────────────────────┬────────────────────────────────────┘   │
│                             │                                        │
│  ┌─ Application Layer ──────▼────────────────────────────────────┐   │
│  │  AgentService  ·  SessionService  ·  SkillService             │   │
│  │  SessionWSOrchestrator  ·  TaskPool                           │   │
│  │  Materialization                                              │   │
│  └──────────────────────────┬────────────────────────────────────┘   │
│                             │                                        │
│  ┌─ Runtime Layer ──────────▼────────────────────────────────────┐   │
│  │  RuntimeBase (ABC)                                            │   │
│  │  ├─ OpenClawGatewayRuntime                                    │   │
│  │  └─ OpenCodeRuntime (WIP)                                     │   │
│  └───────────────────────────┬────────────────────────────────────┘  │
│                              │                                       │
│  ┌─ Adapter Layer ──────────▼──────────────┐   ┌─ Infra Layer ───┐   │
│  │ OpenClawAdapter  ·  RuntimeRegistry      │  │  GatewayClient  │   │
│  └──────────────────────────────────────────┘  │  (WS RPC)       │   │
│                                                 └────────┬────────┘  │
└──────────────────────────────────────────────────────────┼───────────┘
                                                           │
                                                     WebSocket RPC
                                                           │
                                                           ▼
                                           ┌──────────────────────────┐
                                           │    OpenClaw Gateway      │
                                           │  (Agent Runtime Platform)│
                                           └──────────────────────────┘
```

---

## 安装指南

### 方式一：pip安装

如果你只需要运行 Witty-Service，无需参与开发，可以直接通过 pip 安装：

```bash
pip install witty-service
```

安装完成后即可使用 CLI 启动服务：

```bash
witty-service --host 0.0.0.0 --port 8000
```

> **前置条件：** Python 3.11 或更高版本

### 方式二：从源码安装

如果你需要参与开发或自定义构建，请从源码安装：

**前置条件：**

| 依赖 | 说明 | 安装方式 |
|------|------|----------|
| Python | 3.11+ | [python.org](https://www.python.org/downloads/) |
| uv | Python 包管理器 | [docs.astral.sh/uv](https://docs.astral.sh/uv/) |
| Docker | 沙箱运行时（可选） | [docker.com](https://www.docker.com/) |

**安装步骤：**

1. 克隆仓库：

```bash
git clone https://gitcode.com/openeuler/witty-service.git
cd witty-service
```

2. 创建虚拟环境并安装依赖：

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

3. 验证安装：

```bash
witty-service --help
```

---

## 快速开始

**1. 启动服务**

```bash
witty-service --host 0.0.0.0 --port 8000
```

**2. 验证服务是否正常运行**

```bash
curl http://127.0.0.1:8000/healthz
```

期望返回：

```json
{"status": "ok"}
```

---

## 配置说明

Witty-Service 通过环境变量进行配置，无需额外配置文件。

### 核心配置

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `AUTH_TOKEN` | API 认证 Token | `dev-token` |
| `WITTY_AGENT_SERVER_APP_DIR` | Local Process 模式下 witty-agent-server 代码目录 | 空 |

### Witty Insight 集成配置

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `WITTY_INSIGHT_ENABLED` | 是否启用 witty-insight 集成 | `true` |
| `WITTY_INSIGHT_BASE_URL` | witty-insight API 地址 | `http://127.0.0.1:7396` |
| `WITTY_INSIGHT_TIMEOUT_SECONDS` | witty-insight 请求超时（秒） | `10` |
| `WITTY_INSIGHT_BEARER_TOKEN` | 调用 witty-insight 时附带的 Bearer Token（可选） | 空 |

默认推荐同机部署并通过 `127.0.0.1` 连接 `witty-insight`。`witty-service`
默认启用 Insight 集成；如果上游 `witty-insight` 当前未启动，`witty-service`
仍可正常启动。若需关闭该集成，可显式设置 `WITTY_INSIGHT_ENABLED=false`。
修改上述环境变量后，需要重启 `witty-service` 进程才能生效。

同机部署示例：

```bash
export WITTY_INSIGHT_BASE_URL=http://127.0.0.1:7396
export WITTY_INSIGHT_TIMEOUT_SECONDS=10

witty-service --host 0.0.0.0 --port 8000
```

分开部署示例：

```bash
export WITTY_INSIGHT_BASE_URL=http://10.0.0.8:7396
export WITTY_INSIGHT_TIMEOUT_SECONDS=10

witty-service --host 0.0.0.0 --port 8000
```

显式关闭 Insight 集成：

```bash
export WITTY_INSIGHT_ENABLED=false

witty-service --host 0.0.0.0 --port 8000
```

### Docker 沙箱配置

| 环境变量 | 说明 | 默认值 |
|----------|------|--------|
| `WITTY_DOCKER_HOST` | Docker 服务监听地址 | `127.0.0.1` |
| `WITTY_DOCKER_IMAGE` | 镜像名（不含 tag） | `witty-agent-server` |
| `WITTY_DOCKER_IMAGE_TAG` | 镜像 tag | `latest` |
| `WITTY_DOCKER_CONTAINER_PORT` | 容器内服务端口 | `8080` |
| `WITTY_DOCKER_CONTAINER_WORKSPACE_PATH` | 容器内工作区路径 | `/witty-workspace` |
| `WITTY_DOCKER_STOP_TIMEOUT` | 容器停止超时（秒） | `10` |

---

## 部署流程

### 测试环境

适用于集成测试和功能验证：

```bash
# 构建 pip 包
uv build

# 安装构建产物
uv pip install dist/witty_service-0.1.0-py3-none-any.whl

# 初始化数据库 schema（推荐）
alembic upgrade head

# 启动服务
witty-service --host 0.0.0.0 --port 8000
```

若仅做临时本地验证，也可显式设置 `WITTY_DATABASE_AUTO_CREATE=true` 让服务启动时自动建表；默认不会自动建表。

运行测试：

```bash
# 单元测试
uv run pytest tests/unit/ -q

# E2E 测试
uv run pytest tests/e2e/ -q

# 全量测试
uv run pytest tests/ -q
```

### 生产环境

**方式一：pip 安装（推荐）**

```bash
pip install witty-service

# 部署新版本前先执行数据库迁移
alembic upgrade head

# 多 worker 启动
witty-service --host 0.0.0.0 --port 8000 --workers 4
```

**方式二：从源码构建**

```bash
git clone https://gitcode.com/openeuler/witty-service.git
cd witty-service
uv build
uv pip install dist/witty_service-0.1.0-py3-none-any.whl

alembic upgrade head

witty-service --host 0.0.0.0 --port 8000 --workers 4
```

### 启动参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `--host` | 绑定的主机地址 | `0.0.0.0` |
| `--port` | 绑定的端口 | `8000` |
| `--log-level` | 日志级别（debug/info/warning/error/critical） | `info` |
| `--reload` | 开发模式自动重载 | `False` |
| `--workers` | 工作进程数 | `1` |

### 生产环境注意事项

- **认证 Token** — 务必通过 `AUTH_TOKEN` 环境变量修改默认值，使用强随机字符串
- **Worker 数量** — 根据 CPU 核心数合理设置 `--workers`
- **反向代理** — 推荐在 Witty-Service 前部署 Nginx 等反向代理，配置 HTTPS 证书
- **进程管理** — 建议使用 systemd 或 Supervisor 管理服务进程，实现自动重启
- **数据库迁移** — 默认不会在启动时自动建表；请优先执行 `alembic upgrade head`，仅开发/测试临时场景才显式设置 `WITTY_DATABASE_AUTO_CREATE=true`

---

## 本地开发

### 环境搭建

```bash
# 1. 克隆仓库
git clone https://gitcode.com/openeuler/witty-service.git
cd witty-service

# 2. 创建虚拟环境并安装依赖
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# 3. 初始化数据库
alembic upgrade head

# 4. 启动开发服务器
uv run uvicorn src.witty_service.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

### 项目结构

```
witty-service/
├── src/
│   ├── witty_service/                # 主服务包
│   │   ├── main.py                   # FastAPI 应用入口
│   │   ├── cli.py                    # CLI 入口
│   │   ├── config.py                 # 配置管理
│   │   ├── api/                      # API 路由层
│   │   │   ├── agents.py             # Agent 相关接口
│   │   │   ├── models.py             # 模型配置接口
│   │   │   ├── skills.py             # 技能管理接口
│   │   │   ├── cve.py                # CVE 漏洞接口
│   │   │   ├── backport.py           # 代码回溯接口
│   │   │   ├── auth.py               # 认证中间件
│   │   │   ├── errors.py             # 统一错误处理
│   │   │   └── schemas.py            # 请求/响应模型
│   │   ├── application/              # 业务逻辑层
│   │   │   ├── agent_manager.py      # Agent 生命周期管理
│   │   │   ├── session_manager.py    # 会话管理
│   │   │   └── skill_manager.py      # 技能管理
│   │   ├── adapter/                  # 适配器层（与 witty-agent-server 通信）
│   │   │   ├── websocket_client.py   # WebSocket 客户端
│   │   │   ├── websocket_protocol.py # WebSocket 协议定义
│   │   │   └── http_client.py        # HTTP 客户端
│   │   ├── sandbox/                  # 沙箱层
│   │   │   ├── base.py               # 沙箱基类
│   │   │   ├── docker.py             # Docker 沙箱
│   │   │   ├── local_process.py      # 本地进程沙箱
│   │   │   ├── e2b.py                # E2B 云沙箱
│   │   │   └── factory.py            # 沙箱工厂
│   │   ├── domain/                   # 领域模型
│   │   ├── persistence/              # 数据持久化
│   │   └── storage/                  # 文件存储
│   └── witty_agent_server/           # Agent 运行时服务
│       ├── app.py                    # FastAPI 应用
│       ├── api/routers/              # API 路由
│       ├── application/services/     # 业务服务
│       │   ├── agent/                # Agent 服务
│       │   ├── session/              # Session 服务
│       │   └── skill/                # 技能服务
│       ├── runtimes/                 # 运行时实现
│       ├── adapters/                 # 运行时适配器
│       └── infra/                    # 基础设施
├── tests/
│   ├── unit/                         # 单元测试
│   └── e2e/                          # 端到端测试
├── alembic/                          # 数据库迁移
├── docs/                             # 文档
├── pyproject.toml                    # 项目配置
└── .github/workflows/                # CI/CD 工作流
```

### 常用开发命令

| 命令 | 说明 |
|------|------|
| `uv run uvicorn src.witty_service.main:create_app --factory --reload` | 启动开发服务器（热重载） |
| `uv run pytest tests/unit/ -q` | 运行单元测试 |
| `uv run pytest tests/e2e/ -q` | 运行 E2E 测试 |
| `uv run pytest tests/ -q` | 运行全量测试 |
| `uv build` | 构建 pip 包 |
| `alembic revision --autogenerate -m "description"` | 生成数据库迁移脚本 |
| `alembic upgrade head` | 执行数据库迁移 |

### 代码贡献流程

1. Fork 本仓库
2. 创建功能分支：`git checkout -b feature/your-feature`
3. 提交更改：`git commit -m 'feat: add your feature'`
4. 推送分支：`git push origin feature/your-feature`
5. 提交 Pull Request

### 开发规范

- **代码风格** — 使用 Ruff（lint + format，line-length=88）替代 Black/Flake8，配置见 `pyproject.toml` 的 `[tool.ruff]`
- **类型检查** — 使用 mypy 进行静态类型检查，配置为 strict 模式
- **提交规范** — 使用语义化提交信息（如 `feat:`、`fix:`、`docs:`、`refactor:`），pre-commit 的 gitlint 钩子会校验格式
- **测试覆盖** — 新增功能需编写对应的单元测试，确保测试通过
- **数据库迁移** — 涉及模型变更时，需生成对应的 Alembic 迁移脚本

### 代码检查（pre-commit）

1. 安装 pre-commit：`uv tool install pre-commit`
2. 安装 git 钩子：`pre-commit install --hook-type pre-commit --hook-type commit-msg`（或沿用仓库习惯 `cp .githooks/pre-commit .git/hooks/pre-commit` 后，再执行 `pre-commit install --hook-type commit-msg` 启用 gitlint 提交信息校验）
3. 提交时自动检查暂存文件；**全量检查需先收口存量**：`uv run ruff check --fix . && uv run ruff format .`，再执行 `pre-commit run --all-files`
4. 检查项：Ruff（lint/format）、Bandit（安全）、Codespell（拼写）、pre-commit-hooks（基础格式与密钥）、markdownlint、ShellCheck（本机安装时生效）、gitlint（提交信息）；Gitleaks（密钥深扫）在网络就绪后启用

---

## 许可证

本项目基于 [MIT 许可证](LICENSE) 开源。

## 支持与反馈

- **问题反馈** — 请在 [Gitcode Issues](https://gitcode.com/openeuler/witty-service/issues) 提交
- **功能建议** — 欢迎通过 Issue 或 Pull Request 参与
- **项目主页** — [https://gitcode.com/openeuler/witty-service](https://gitcode.com/openeuler/witty-service)
