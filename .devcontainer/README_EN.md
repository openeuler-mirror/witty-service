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

First build takes ~2-3 minutes (subsequent starts use cache and complete in seconds; first-build time is dominated by base-image pulls and mirror reachability). The `postCreateCommand` automatically:

- Fixes workspace file permissions (Linux host bind-mount)
- Creates `agent-workspaces/` with correct ownership (matching agent container's `witty` user, uid 1000)
- Creates `.env` from `.env.example` template
- Runs `uv sync --extra dev` to install Python dependencies
- Runs `alembic upgrade head` to initialize the SQLite database
- Enables the repo's tracked Git commit hooks (container-global `core.hooksPath=.githooks`, see below)
- Prints Python/Node/uv/pre-commit/Docker version info

Start the dev server:

```bash
uv run uvicorn witty_service.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

Or press `F5` to use the existing debugpy launch configurations.

## Included Toolchain

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.11 | Aligned with mypy/Dockerfile/CI |
| Node.js | 22 | npm + npx, aligned with production Dockerfile base image (`node:22.23.3-slim`) |
| uv | 0.8.8 | Python package manager |
| openclaw | 2026.6.5 | Agent runtime CLI (matches production `OPENCLAW_VERSION`) |
| opencode-ai | 1.17.20 | OpenCode runtime CLI |
| wittyhub | latest | Skill management |
| Docker CLI | - | For building agent images and debugging containers |
| pre-commit | 4.x | Preinstalled at build time, drives the repo's tracked `.githooks/` commit hooks |

## Git Commit Hooks (pre-commit)

`pre-commit install` is deliberately **not** run inside the container: it would write into the host's `.git/hooks` (bind-mounted into the container) and bake container-side interpreter paths into the host checkout. Instead:

- The [Dockerfile](Dockerfile) preinstalls `pre-commit` into the system path at build time
- post-create sets a container-global `core.hooksPath=.githooks` (stored in `~/.gitconfig` inside the `witty-home` volume, never in the host working tree)
- The hook scripts are the repo-tracked [.githooks/pre-commit](../.githooks/pre-commit) and [.githooks/commit-msg](../.githooks/commit-msg), running `pre-commit run` and `pre-commit run --hook-stage commit-msg` (gitlint validates the commit message)

Result: `git commit` inside the container checks staged files against the same [.pre-commit-config.yaml](../.pre-commit-config.yaml) the host uses, and the host's `.git/hooks` is never rewritten by the container — once `core.hooksPath` is set, pre-commit refuses to `install` at all.

Host-side hook installation is unchanged; see the [main README](../README_EN.md).

## Ports

| Port | Service | Notes |
|------|---------|-------|
| 8000 | Witty Service Dev Server | `uvicorn --port 8000` |
| 5678 | Debugpy Attach | Debugger attach port (`.vscode/launch.json`) |
| 8080 | Agent Server (in-container) | witty-agent-server listens on this inside agent containers |
| 7396 | Witty Insight (host) | Optional integration, reachable via host networking |

> **Note**: The container uses `--network host`, so ports appear directly on the host without forwarding.

## Joint Debugging with the Frontend (PolyMind)

This devcontainer runs the backend only. The PolyMind frontend lives in `/root/polymind` with its own devcontainer. Both use `--network host`, so **running them as separate containers** is enough to integrate via `127.0.0.1` — no port forwarding or extra networking needed:

| Side | Directory | Command | Port |
|------|-----------|---------|------|
| Backend | `/root/witty-service` | `uv run uvicorn witty_service.main:create_app --factory --host 0.0.0.0 --port 8000 --reload` | 8000 |
| Frontend | `/root/polymind` | `pnpm dev` | 3000 |

`polymind/.env` points at this service by default: `NEXT_PUBLIC_AGENTD_API_URL=http://127.0.0.1:8000`. This service allows CORS (`allow_origins=["*"]`), so the browser can call `127.0.0.1:8000` from `localhost:3000` directly.

> The backend must listen on `--host 0.0.0.0`; binding to loopback only will make it unreachable from the frontend container.

## Environment Variables

Two layers provide environment variables:

1. **`containerEnv`** (`devcontainer.json`): covers VS Code terminals, debug sessions, and tasks
2. **`.env` file**: auto-generated from `.env.example` on first creation, for `docker exec` / manual shell usage

> Existing `.env` files are never overwritten — your custom configuration is always preserved.

## Caching Strategy

One named volume persists across rebuilds:

| Volume | Mount Point | Contents |
|--------|-------------|----------|
| `witty-home` | `/home/vscode/` | Python venv (`.venv/`), uv cache, npm cache, `.witty/` (SQLite DB + logs), `.openclaw/`, `.opencode/` |

It survives container rebuilds, making `uv sync` nearly instant on subsequent starts.

The venv deliberately lives at `/home/vscode/.venv` (set by the image via `UV_PROJECT_ENVIRONMENT`)
instead of in the workspace: when a named volume is first mounted, its ownership is inherited from the
content at the mount target in the image. `/home/vscode` is owned by the vscode user, while a
workspace-derived path is decided by the bind mount / host. Mounting the venv volume at
`<workspace>/.venv` therefore yields a `root:root` volume that the non-root lifecycle scripts cannot
write to (`uv` reports `.venv/CACHEDIR.TAG: Permission denied`), leaving `sudo chown` as the only remedy.

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
echo "$UV_PROJECT_ENVIRONMENT"   # expected: /home/vscode/.venv (inside the witty-home volume)
uv sync --extra dev              # re-run manually to see the full error (post-create hides details)
```

If it reports `Permission denied`, the volume was turned `root`-owned by something external (this does
not happen in the normal flow); take ownership back as described in the caching strategy section.

### Commits inside the container skip the checks

```bash
# Verify pre-commit and that hooksPath points at the tracked .githooks
command -v pre-commit && pre-commit --version
git config --show-origin core.hooksPath   # expect: file:/home/vscode/.gitconfig  .githooks
ls -l .githooks/                          # pre-commit and commit-msg must be executable

# Restore it if ~/.gitconfig was overwritten manually
git config --global core.hooksPath .githooks
# Or re-run the setup script
bash .devcontainer/scripts/post-create.sh
```

> If `pre-commit` is missing (the PyPI mirror was unreachable during the image build), post-create
> reinstalls it with `sudo pip install`, matching how the image installs it; if that still fails, run
> `sudo pip install --no-cache-dir pre-commit` manually and set `core.hooksPath` again.

### China network acceleration

This devcontainer defaults to China-friendly mirrors and adds retry/timeout settings to absorb the slow downloads and connection drops common on CN networks:

| Layer | Mirror | Notes |
|-------|--------|-------|
| **apt** | `http://mirrors.aliyun.com` | Replaces the default `deb.debian.org` (slow and drop-prone from CN). HTTP is intentional: slim base images ship without a CA bundle, so https would fail before `ca-certificates` is installed; apt packages are GPG-signed, so HTTP does not weaken integrity |
| **PyPI** | `https://mirrors.aliyun.com/pypi/simple/` | Baked into `uv.lock` and `pyproject.toml [tool.uv]`; `pip` uses `PIP_INDEX_URL` |
| **npm** | `https://registry.npmmirror.com` | Written to the global npmrc, applying to all users and later `npm install`s |

Resilience settings:

- **pip**: `PIP_DEFAULT_TIMEOUT=120`, `PIP_RETRIES=5`
- **npm**: `fetch-retries=5`, `fetch-retry-maxtimeout=120000`, `fetch-timeout=300000` (global npmrc)
- **uv**: `UV_HTTP_TIMEOUT=120`, `UV_CONCURRENT_DOWNLOADS=16` (fewer parallel downloads to reduce drops)

To switch mirrors, edit the corresponding build args (`APT_MIRROR` / `NPM_REGISTRY`) in `devcontainer.json`.

### Base image pull is slow or blocked

The first build pulls `python:3.11-slim` and `node:22.23.3-slim` from Docker Hub — the slowest and most disconnect-prone step. Configure a registry mirror for the Docker daemon (applies to every image):

```jsonc
// /etc/docker/daemon.json
{ "registry-mirrors": ["https://<your-mirror>"] }
```

Then `sudo systemctl restart docker`.

## Configuration Notes

- **Python 3.11**: matches mypy `python_version`, production Dockerfile, and CI
- **Node.js 22**: matches the production Dockerfile base image (`node:22.23.3-slim`)
- **black line-length=88**: matches `[tool.black]` in pyproject.toml
- **flake8**: extension args set explicitly (flake8 does not read `pyproject.toml`)
- **mypy strict**: matches `[tool.mypy]` in pyproject.toml

## More Information

- [VS Code Dev Containers documentation](https://code.visualstudio.com/docs/devcontainers/containers)
- [Dev Container Features reference](https://containers.dev/features)
- [Witty Service README](../README.md)
