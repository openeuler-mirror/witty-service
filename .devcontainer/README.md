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

容器首次构建约需 2-3 分钟（后续启动使用缓存，秒级完成；首次构建耗时主要取决于能否拉取到基础镜像和镜像源的连通性）。`postCreateCommand` 自动完成：

- 修复工作区文件权限（Linux 宿主机 bind-mount 场景）
- 创建 `agent-workspaces/` 并设置权限（对齐 agent 容器的 `witty` 用户 uid 1000）
- 基于 `.env.example` 模板创建 `.env` 配置文件
- 执行 `uv sync --extra dev` 安装 Python 依赖
- 执行 `alembic upgrade head` 初始化 SQLite 数据库
- 启用仓库自带的 Git 提交钩子（容器内 `core.hooksPath=.githooks`，见下节）
- 显示 Python/Node/uv/pre-commit/Docker 版本信息

启动开发服务器：

```bash
uv run uvicorn witty_service.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

或按 `F5` 使用已有的 debugpy 调试配置启动。

## 容器内包含的工具链

| 工具 | 版本 | 说明 |
|------|------|------|
| Python | 3.11 | 对齐 mypy/Dockerfile/CI 目标版本 |
| Node.js | 22 | npm + npx，对齐生产 Dockerfile 基础镜像（`node:22.23.3-slim`） |
| uv | 0.8.8 | Python 包管理器 |
| openclaw | 2026.6.5 | Agent 运行时 CLI（对齐生产 Dockerfile `OPENCLAW_VERSION`） |
| opencode-ai | 1.17.20 | OpenCode 运行时 CLI |
| wittyhub | latest | Skill 管理工具 |
| Docker CLI | - | 用于构建 agent 镜像和调试容器 |
| pre-commit | 4.x | 构建期预装，驱动仓库自带的 `.githooks/` 提交钩子 |

## Git 提交钩子（pre-commit）

容器内**不执行** `pre-commit install`：那会把 hook 写进 bind-mount 进容器的宿主机 `.git/hooks`，并把容器内的解释器路径写进宿主机仓库。改用以下机制：

- **构建期**由 [Dockerfile](Dockerfile) 预装 `pre-commit` 到系统路径
- **post-create** 在容器内设置全局 `core.hooksPath=.githooks`（落在 `witty-home` 卷的 `~/.gitconfig`，不写宿主机工作区）
- **钩子脚本**用仓库 tracked 的 [.githooks/pre-commit](../.githooks/pre-commit) 与 [.githooks/commit-msg](../.githooks/commit-msg)，分别执行 `pre-commit run` 和 `pre-commit run --hook-stage commit-msg`（gitlint 校验提交信息）

效果：容器内 `git commit` 自动检查暂存文件，检查项与宿主机同源（同一份 [.pre-commit-config.yaml](../.pre-commit-config.yaml)）；宿主机 `.git/hooks` 不会被容器改写——`core.hooksPath` 设置后 pre-commit 会主动拒绝 `install`，容器内误执行也改不动 `.git`。

宿主机侧的钩子安装方式不变，见 [主 README](../README.md)。

## 端口说明

| 端口 | 服务 | 说明 |
|------|------|------|
| 8000 | Witty Service Dev Server | `uvicorn --port 8000` 开发服务器 |
| 5678 | Debugpy Attach | 调试器附加端口（配合 `.vscode/launch.json`） |
| 8080 | Agent Server（容器内） | Agent 容器内 witty-agent-server 监听端口 |
| 7396 | Witty Insight（宿主机） | 可选集成服务，通过 host 网络可达 |

> **注意**：容器使用 `--network host`，所有端口直接出现在宿主机上，无需端口转发。

## 与前端（PolyMind）联调

本 devcontainer 只跑后端。前端 PolyMind 在 `/root/polymind` 有独立的 devcontainer，两者都使用 `--network host`，因此**各开各的容器**即可通过 `127.0.0.1` 直接联调，无需端口转发或额外网络配置：

| 侧 | 目录 | 启动命令 | 端口 |
|------|------|----------|------|
| 后端 | `/root/witty-service` | `uv run uvicorn witty_service.main:create_app --factory --host 0.0.0.0 --port 8000 --reload` | 8000 |
| 前端 | `/root/polymind` | `pnpm dev` | 3000 |

前端 `polymind/.env` 默认指向本服务：`NEXT_PUBLIC_AGENTD_API_URL=http://127.0.0.1:8000`。本服务已放开 CORS（`allow_origins=["*"]`），浏览器从 `localhost:3000` 跨域访问 `127.0.0.1:8000` 可直接连通。

> 后端务必以 `--host 0.0.0.0` 监听，否则仅绑定回环地址，前端容器将无法访问。

## 环境变量

容器通过两层机制提供环境变量：

1. **`containerEnv`**（`devcontainer.json`）：覆盖 VS Code 终端、调试会话和任务
2. **`.env` 文件**：容器首次创建时从 `.env.example` 自动生成，用于 `docker exec` / 手动 shell

> `.env` 已存在时不会被覆盖，始终保留你的自定义配置。

## 缓存策略

项目使用一个命名卷加速重建：

| 卷 | 挂载点 | 内容 |
|------|------|------|
| `witty-home` | `/home/vscode/` | Python venv（`.venv/`）、uv 缓存、npm 缓存、`.witty/`（SQLite DB + logs）、`.openclaw/`、`.opencode/` |

该卷在容器重建后仍然保留，使 `uv sync` 几乎瞬时完成。

venv 刻意放在 `/home/vscode/.venv`（由镜像通过 `UV_PROJECT_ENVIRONMENT` 指定），**不放在工作区**：
命名卷首次挂载时属主继承自"挂载目标在工作镜像中的内容"，`/home/vscode` 属于 vscode 用户，
而工作区路径只由 bind mount / 宿主机决定。若把 venv 卷挂到 `<工作区>/.venv`，新建卷会变成 `root:root`，
非 root 的 lifecycle 脚本无法写入（`uv` 会报 `.venv/CACHEDIR.TAG: Permission denied`），
只能靠 `sudo chown` 补救。

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
echo "$UV_PROJECT_ENVIRONMENT"   # 期望：/home/vscode/.venv（witty-home 卷内）
uv sync --extra dev              # 手动重跑，查看完整报错（post-create 会静默失败的细节）
```

若报 `Permission denied`，说明该卷被外部因素改成了 root 所有（正常流程不会发生），
按缓存策略一节取回属主即可。

### 容器内提交未触发检查

```bash
# 确认 pre-commit 可用、hooksPath 指向仓库自带的 .githooks
command -v pre-commit && pre-commit --version
git config --show-origin core.hooksPath   # 期望：file:/home/vscode/.gitconfig  .githooks
ls -l .githooks/                          # 需有可执行的 pre-commit 与 commit-msg

# 配置被覆盖（如手动替换了 ~/.gitconfig）时重设
git config --global core.hooksPath .githooks
# 或重跑初始化脚本
bash .devcontainer/scripts/post-create.sh
```

> 若 `pre-commit` 缺失（镜像构建时 PyPI 源不可达），post-create 会以 `sudo pip install` 补装（与镜像内的
> 安装方式一致）；仍失败可手动执行 `sudo pip install --no-cache-dir pre-commit` 后重设 `core.hooksPath`。

### 国内网络加速

此 devcontainer 默认使用国内镜像源，并针对国内网络常见的**下载慢、连接中断**做了重试/超时配置：

| 层 | 镜像源 | 说明 |
|------|--------|------|
| **apt** | `http://mirrors.aliyun.com` | 替代默认 `deb.debian.org`（默认源在国内慢且易断流）。用 http 是因为 slim 基础镜像没有 CA 证书，https 会在安装 `ca-certificates` 前握手失败；apt 包有 GPG 签名校验，http 不降低完整性 |
| **PyPI** | `https://mirrors.aliyun.com/pypi/simple/` | 已写入 `uv.lock` 与 `pyproject.toml [tool.uv]`，`pip` 侧由 `PIP_INDEX_URL` 覆盖 |
| **npm** | `https://registry.npmmirror.com` | 写入全局 npmrc，容器内所有用户、后续 `npm install` 均生效 |

断连相关的韧性配置：

- **apt**：`apt-get` 默认自带重试
- **pip**：`PIP_DEFAULT_TIMEOUT=120`、`PIP_RETRIES=5`
- **npm**：`fetch-retries=5`、`fetch-retry-maxtimeout=120000`、`fetch-timeout=300000`（写入全局 npmrc）
- **uv**：`UV_HTTP_TIMEOUT=120`、`UV_CONCURRENT_DOWNLOADS=16`（降低并发，减少断流）

如需切换镜像源，修改 `devcontainer.json` 中对应的 build args（`APT_MIRROR` / `NPM_REGISTRY`）。

### 基础镜像拉取慢或被墙

首次构建会从 Docker Hub 拉取 `python:3.11-slim` 和 `node:22.23.3-slim`，这是**最慢、最容易断连**的一步。给 Docker daemon 配置 registry mirror 即可（一次配置对所有镜像生效）：

```jsonc
// /etc/docker/daemon.json
{ "registry-mirrors": ["https://<your-mirror>"] }
```

改完执行 `sudo systemctl restart docker`。

## 配置说明

- **Python 3.11**：与 mypy `python_version`、生产 Dockerfile、CI 保持一致
- **Node.js 22**：与生产 Dockerfile 基础镜像（`node:22.23.3-slim`）版本一致
- **black line-length=88**：对齐 `pyproject.toml` 的 `[tool.black]` 配置
- **flake8**：扩展参数显式设置（flake8 不读取 `pyproject.toml`）
- **mypy strict**：对齐 `pyproject.toml` 的 `[tool.mypy]` 配置

## 更多信息

- [VS Code Dev Containers 文档](https://code.visualstudio.com/docs/devcontainers/containers)
- [Dev Container Features 参考](https://containers.dev/features)
- [Witty Service 项目 README](../README.md)
