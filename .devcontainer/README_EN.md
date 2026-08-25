# Witty Service DevContainer

This directory contains [VS Code Dev Containers](https://code.visualstudio.com/docs/devcontainers/containers) configuration for containerized Witty Service development.

> 📖 中文版：[README.md](README.md)

## Prerequisites

- **Linux host** (macOS/Windows Docker VM does not support `--network host` and same-path bind mounts; Agent Docker sandbox will be limited)
- [Docker](https://docs.docker.com/get-docker/) (Docker Engine with daemon running)
- [VS Code](https://code.visualstudio.com/) + [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers)

## Quick Start

```bash
# 1. Clone the repo
git clone <repo-url> witty-service
cd witty-service

# 2. Open in VS Code
code .

# 3. Click the popup or F1 → "Dev Containers: Reopen in Container"
```

First build takes ~2-3 minutes (subsequent starts use cache and complete in seconds). The `onCreateCommand` automatically:

- Fixes workspace file permissions (Linux host bind-mount)
- Creates `agent-workspaces/` with correct ownership (matching agent container's `witty` user, uid 1000)
- Creates `.env` from `.env.example` template
- Runs `uv sync --extra dev` to install Python dependencies
- Runs `alembic upgrade head` to initialize the SQLite database
- Prints Python/Node/uv/Docker version info

Start the dev server:

```bash
uv run uvicorn witty_service.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

Or press `F5` to use the existing debugpy launch configurations.

## Included Toolchain

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.11 | Aligned with mypy/Dockerfile/CI |
| Node.js | 24 | npm + npx, aligned with production Dockerfile |
| uv | 0.8.8 | Python package manager |
| openclaw | 2026.7.1-2 | Agent runtime CLI |
| opencode-ai | 1.17.20 | OpenCode runtime CLI |
| wittyhub | latest | Skill management |
| Docker CLI | - | For building agent images and debugging containers |

## Ports

| Port | Service | Notes |
|------|---------|-------|
| 8000 | Witty Service Dev Server | `uvicorn --port 8000` |
| 5678 | Debugpy Attach | Debugger attach port (`.vscode/launch.json`) |
| 8080 | Agent Server (in-container) | witty-agent-server listens on this inside agent containers |
| 7396 | Witty Insight (host) | Optional integration, reachable via host networking |

> **Note**: The container uses `--network host`, so ports appear directly on the host without forwarding.

## Environment Variables

Two layers provide environment variables:

1. **`containerEnv`** (`devcontainer.json`): covers VS Code terminals, debug sessions, and tasks
2. **`.env` file**: auto-generated from `.env.example` on first creation, for `docker exec` / manual shell usage

> Existing `.env` files are never overwritten — your custom configuration is always preserved.

## Caching Strategy

Two named volumes persist across rebuilds:

| Volume | Mount Point | Contents |
|--------|-------------|----------|
| `witty-venv` | `.venv/` | Python virtual environment and all dependencies |
| `witty-home` | `/home/vscode/` | uv cache, npm cache, `.witty/` (SQLite DB + logs), `.openclaw/`, `.opencode/` |

These volumes survive container rebuilds, making `uv sync` nearly instant on subsequent starts.

## Building Agent Images

The Docker sandbox feature requires pre-built agent images. The container checks image status on startup:

```bash
# Build OpenClaw runtime image
docker build --target openclaw -t witty-agent-server:openclaw .

# Build OpenCode runtime image
docker build --target opencode -t witty-agent-server:opencode .
```

Images are tagged as `witty-agent-server:{adapter_type}`, matching the `WITTY_DOCKER_IMAGE` + `image_tag` logic in the codebase.

## Running Tests

```bash
# Unit tests (no Docker required)
uv run pytest tests/unit/ -q

# E2E tests
uv run pytest tests/e2e/ -q

# Code quality
uv run black --check src tests
uv run flake8 src tests --max-line-length=88 --extend-ignore=E203,W503
uv run mypy src
```

## Known Limitations

### Linux-only

Host networking (`--network host`) and same-path bind mounts are unavailable in macOS/Windows Docker Desktop VMs. On these platforms:

- Unit tests and local_process sandbox work normally
- Docker sandbox is unavailable (agent container ports unreachable via 127.0.0.1)

### Host UID ≠ 1000

Both the dev user (`vscode`) and agent container user (`witty`) hardcode uid 1000. If your host UID differs:

- `updateRemoteUserUID: true` corrects repo file ownership
- `agent-workspaces/` permissions are fixed by lifecycle scripts
- In extreme cases, agent workspace writes may fail; unit tests and local_process are unaffected

## Troubleshooting

### Workspace not writable

```bash
sudo chown -R vscode:vscode /path/to/witty-service
```

### Docker socket unavailable

```bash
# Verify host Docker daemon is running
docker info

# Check socket mount
ls -la /var/run/docker-host/docker.sock
```

### uv sync fails

```bash
# Clean volumes and rebuild
docker volume rm witty-venv witty-home
# Then Rebuild Container in VS Code
```

### China network acceleration

This devcontainer defaults to China-friendly mirrors:

- **npm**: `https://registry.npmmirror.com`
- **PyPI**: `https://mirrors.aliyun.com/pypi/simple/` (baked into uv.lock)

To switch, modify the `NPM_REGISTRY` build arg in `devcontainer.json`.

## Configuration Notes

- **Python 3.11**: matches mypy `python_version`, production Dockerfile, and CI
- **Node.js 24**: matches the production Dockerfile base image version
- **black line-length=88**: matches `[tool.black]` in pyproject.toml
- **flake8**: extension args set explicitly (flake8 does not read `pyproject.toml`)
- **mypy strict**: matches `[tool.mypy]` in pyproject.toml

## More Information

- [VS Code Dev Containers documentation](https://code.visualstudio.com/docs/devcontainers/containers)
- [Dev Container Features reference](https://containers.dev/features)
- [Witty Service README](../README.md)
