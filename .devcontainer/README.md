# Witty Service DevContainer 开发环境

本目录包含 [VS Code Dev Containers](https://code.visualstudio.com/docs/devcontainers/containers) 配置文件，让你在容器化的开发环境中快速开始 Witty Service 开发。

## 前置条件

- **Linux 宿主机**（macOS/Windows 的 Docker VM 不支持 `--network host` 和同路径 bind-mount，Agent Docker sandbox 功能受限）
- [Docker](https://docs.docker.com/get-docker/)（Docker Engine，daemon 需运行中）
- [VS Code](https://code.visualstudio.com/) + [Dev Containers 扩展](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)

## 快速开始

```bash
# 1. 克隆仓库
git clone <repo-url> witty-service
cd witty-service

# 2. 用 VS Code 打开项目
code .

# 3. 点击右下角提示或按 F1 → "Dev Containers: Reopen in Container"
```

容器首次构建约需 2-3 分钟（后续启动使用缓存，秒级完成）。`onCreateCommand` 自动完成：

- 修复工作区文件权限（Linux 宿主机 bind-mount 场景）
- 创建 `agent-workspaces/` 并设置权限（对齐 agent 容器的 `witty` 用户 uid 1000）
- 基于 `.env.example` 模板创建 `.env` 配置文件
- 执行 `uv sync --extra dev` 安装 Python 依赖
- 执行 `alembic upgrade head` 初始化 SQLite 数据库
- 显示 Python/Node/uv/Docker 版本信息

启动开发服务器：

```bash
uv run uvicorn witty_service.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

或按 `F5` 使用已有的 debugpy 调试配置启动。

## 容器内包含的工具链

| 工具 | 版本 | 说明 |
|------|------|------|
| Python | 3.11 | 对齐 mypy/Dockerfile/CI 目标版本 |
| Node.js | 24 | npm + npx，对齐生产 Dockerfile |
| uv | 0.8.8 | Python 包管理器 |
| openclaw | 2026.7.1-2 | Agent 运行时 CLI |
| opencode-ai | 1.17.20 | OpenCode 运行时 CLI |
| wittyhub | latest | Skill 管理工具 |
| Docker CLI | - | 用于构建 agent 镜像和调试容器 |

## 端口说明

| 端口 | 服务 | 说明 |
|------|------|------|
| 8000 | Witty Service Dev Server | `uvicorn --port 8000` 开发服务器 |
| 5678 | Debugpy Attach | 调试器附加端口（配合 `.vscode/launch.json`） |
| 8080 | Agent Server（容器内） | Agent 容器内 witty-agent-server 监听端口 |
| 7396 | Witty Insight（宿主机） | 可选集成服务，通过 host 网络可达 |

> **注意**：容器使用 `--network host`，所有端口直接出现在宿主机上，无需端口转发。

## 环境变量

容器通过两层机制提供环境变量：

1. **`containerEnv`**（`devcontainer.json`）：覆盖 VS Code 终端、调试会话和任务
2. **`.env` 文件**：容器首次创建时从 `.env.example` 自动生成，用于 `docker exec` / 手动 shell

> `.env` 已存在时不会被覆盖，始终保留你的自定义配置。

## 缓存策略

项目使用两个命名卷加速重建：

| 卷 | 挂载点 | 内容 |
|------|------|------|
| `witty-venv` | `.venv/` | Python 虚拟环境和所有依赖 |
| `witty-home` | `/home/vscode/` | uv 缓存、npm 缓存、`.witty/`（SQLite DB + logs）、`.openclaw/`、`.opencode/` |

这两个卷在容器重建后仍然保留，使 `uv sync` 几乎瞬时完成。

## Agent 镜像构建

Docker sandbox 功能需要预先构建 agent 镜像。容器首次启动后会检查镜像状态：

```bash
# 构建 OpenClaw 运行时镜像
docker build --target openclaw -t witty-agent-server:openclaw .

# 构建 OpenCode 运行时镜像
docker build --target opencode -t witty-agent-server:opencode .
```

镜像 tag 格式为 `witty-agent-server:{adapter_type}`，与代码中的 `WITTY_DOCKER_IMAGE` + `image_tag` 逻辑一致。

## 运行测试

```bash
# 单元测试（无 Docker 依赖）
uv run pytest tests/unit/ -q

# E2E 测试
uv run pytest tests/e2e/ -q

# 代码格式检查
uv run black --check src tests
uv run flake8 src tests --max-line-length=88 --extend-ignore=E203,W503

# 类型检查
uv run mypy src
```

## 已知限制

### Linux-only

Host 网络（`--network host`）和同路径 bind-mount 在 macOS/Windows 的 Docker Desktop VM 中不可用。在这些平台上：

- 单元测试和 local_process sandbox 可正常工作
- Docker sandbox 功能不可用（agent 容器端口无法通过 127.0.0.1 访问）

### Host UID ≠ 1000

Dev 用户（`vscode`）和 agent 容器用户（`witty`）均硬编码为 uid 1000。如果宿主机用户 UID 不是 1000：

- `updateRemoteUserUID: true` 会修正 repo 文件的所有权
- `agent-workspaces/` 的写入权限由 post-create/post-start 脚本自动修复
- 极端情况下 agent workspace 写入可能失败，单元测试和 local_process 流程不受影响

## 故障排查

### 工作区不可写

```bash
sudo chown -R vscode:vscode /path/to/witty-service
```

### Docker socket 不可用

```bash
# 确认宿主机 Docker daemon 正在运行
docker info

# 检查 socket 挂载
ls -la /var/run/docker-host/docker.sock
```

### uv sync 失败

```bash
# 清理 venv 卷后重建
docker volume rm witty-venv witty-home
# 然后在 VS Code 中 Rebuild Container
```

### 国内网络加速

此 devcontainer 默认使用国内镜像源：

- **npm**：`https://registry.npmmirror.com`
- **PyPI**：`https://mirrors.aliyun.com/pypi/simple/`（uv.lock 已内置）

如需切换，修改 `devcontainer.json` 中的 `NPM_REGISTRY` build arg。

## 配置说明

- **Python 3.11**：与 mypy `python_version`、生产 Dockerfile、CI 保持一致
- **Node.js 24**：与生产 Dockerfile 基础镜像版本一致
- **black line-length=88**：对齐 `pyproject.toml` 的 `[tool.black]` 配置
- **flake8**：扩展参数显式设置（flake8 不读取 `pyproject.toml`）
- **mypy strict**：对齐 `pyproject.toml` 的 `[tool.mypy]` 配置

## 更多信息

- [VS Code Dev Containers 文档](https://code.visualstudio.com/docs/devcontainers/containers)
- [Dev Container Features 参考](https://containers.dev/features)
- [Witty Service 项目 README](../README.md)
