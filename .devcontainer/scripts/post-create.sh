#!/usr/bin/env bash
# witty-service devcontainer first-run setup. Idempotent — safe on every container creation.
set -euo pipefail
# Derive workspace folder from script path.
# Script is at .devcontainer/scripts/post-create.sh — go up 3 levels to workspace root.
# Invoked with absolute path from devcontainer.json (containerWorkspaceFolder variable).
WORKSPACE_DIR="$(dirname "$(dirname "$(dirname "$0")")")"
cd "$WORKSPACE_DIR"

echo "=== Witty Service devcontainer: post-create setup ==="

# 1. Fix workspace ownership on Linux hosts where bind mounts preserve host UIDs.
if [ ! -w . ]; then
  echo "[fix] Workspace not writable — adjusting ownership..."
  sudo chown -R "$(id -u):$(id -g)" "$(pwd)" 2>/dev/null || {
    echo "WARNING: Could not adjust workspace ownership."
    echo "Run manually:  sudo chown -R vscode:vscode $(pwd)"
  }
fi

# 2. Ensure agent-workspaces exists and is writable by uid 1000.
#    Agent containers run as 'witty' (uid 1000) and mount workspace paths via the host daemon.
#    Files created by the dev user (uid 1000 after updateRemoteUserUID) must be writable by
#    the agent container's user, and vice versa.
mkdir -p agent-workspaces
sudo chown -R 1000:1000 agent-workspaces 2>/dev/null || true

# 3. Create a dev-ready .env from .env.example if absent (never overwrite user config).
if [ ! -f .env ]; then
  cp .env.example .env
  cat >> .env << 'EOF'

# Devcontainer overrides (containerEnv already covers VS Code terminals/tasks)
WITTY_WORKSPACE_ROOT=$(pwd)/agent-workspaces
WITTY_DOCKER_HOST=127.0.0.1
WITTY_DOCKER_IMAGE=witty-agent-server
WITTY_INSIGHT_ENABLED=false
WITTY_LOG_LEVEL=DEBUG
EOF
  echo "[ok] Created .env from .env.example + devcontainer overrides"
else
  echo "[ok] .env already exists — keeping existing configuration"
fi

# 4. Configure bash to auto-load .env in interactive shells.
#    The project does not use python-dotenv; this covers docker exec / shell usage.
if ! grep -q 'set -a; \[ -f .env \]' /home/vscode/.bashrc 2>/dev/null; then
  cat >> /home/vscode/.bashrc << 'EOF'

# Auto-load .env in interactive shells (devcontainer)
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
EOF
fi

# 5. Grant vscode access to the host Docker socket via a group matching the
#    socket's host group — instead of a world-writable chmod 666, which would
#    alter the host socket's permissions and leave them open to every local user.
if [ -S /var/run/docker-host/docker.sock ]; then
  SOCK_GID="$(stat -c '%g' /var/run/docker-host/docker.sock 2>/dev/null || echo '')"
  if [ -n "$SOCK_GID" ]; then
    if ! getent group "$SOCK_GID" >/dev/null 2>&1; then
      sudo groupadd -g "$SOCK_GID" docker-host 2>/dev/null || true
    fi
    DOCKER_GROUP="$(getent group "$SOCK_GID" | cut -d: -f1 2>/dev/null || echo docker-host)"
    sudo usermod -aG "$DOCKER_GROUP" vscode 2>/dev/null || true
  fi
fi

# 6. Install Python dependencies via uv.
#    uv.lock bakes Aliyun mirror URLs, so no additional registry config is needed.
#    The venv lives at $UV_PROJECT_ENVIRONMENT (/home/vscode/.venv, inside the witty-home
#    volume) and is pre-created by the image with vscode ownership — so uv sync needs no
#    privileged ownership fix here.
#    NOTE: uv.lock is a git-tracked, bind-mounted file — never delete or regenerate it here,
#    otherwise transient failures would silently leak unrelated dependency upgrades into commits.
echo "[...] Installing Python dependencies with uv..."
if ! uv sync --extra dev 2>/dev/null; then
  echo "[warn] uv sync failed — retrying without the lock file (uv.lock left intact)..."
  uv sync --extra dev --no-lock 2>/dev/null || {
    echo "[ERROR] Failed to install Python dependencies."
    echo "  Run manually: uv sync --extra dev"
  }
fi
echo "[ok] Python dependencies installed"

# 7. Ensure runtime directories exist (used by witty-service for DB, logs, etc.).
#    /home/vscode is owned by the dev user, so mkdir alone yields correct ownership.
echo "[...] Creating runtime directories..."
mkdir -p /home/vscode/.witty/db /home/vscode/.witty/logs
echo "[ok] Runtime directories ready"

# 8. Initialize the database with Alembic migrations.
#    Use an absolute DATABASE_URL (sqlite://// = 4 slashes = absolute path) so the DB always
#    lands in the witty-home volume, independent of the shell's working directory.
echo "[...] Running Alembic migrations..."
WITTY_DATABASE_URL="sqlite:////home/vscode/.witty/db/witty_service.sqlite3" \
    uv run alembic upgrade head 2>/dev/null || {
  echo "[warn] Alembic migration failed — DB will be auto-created on first run"
}
echo "[ok] Database initialized"

# 9. Mark the bind-mounted workspace as a safe.directory for the current dev user.
git config --global --add safe.directory "$(pwd)"

# 10. Enable the repo's tracked Git hooks.
#     This script runs as the dev user (remoteUser), so --global targets ~/.gitconfig directly.
echo "[...] Enabling repo-tracked Git hooks..."
if ! command -v pre-commit >/dev/null 2>&1; then
  echo "[warn] pre-commit missing (unreachable PyPI mirror during image build?) — installing..."
  sudo pip install --no-cache-dir pre-commit 2>/dev/null || \
    echo "[ERROR] pre-commit install failed — commits in this container will skip checks"
fi
if command -v pre-commit >/dev/null 2>&1; then
  git config --global core.hooksPath .githooks
  echo "[ok] core.hooksPath=.githooks ($(pre-commit --version) at $(command -v pre-commit))"
fi

# 11. Print toolchain versions for parity check with CI/Dockerfile.
echo "--- Toolchain versions ---"
echo "Python:  $(python --version)"
echo "Node:    $(node --version)"
echo "npm:     $(npm --version)"
echo "uv:      $(uv --version)"
echo "openclaw: $(openclaw --version 2>&1 | head -1 || echo 'not found')"
echo "pre-commit: $(pre-commit --version 2>/dev/null || echo 'not found')"

# 12. Docker availability check.
if docker info &>/dev/null; then
  echo "Docker:  available ($(docker info --format '{{.ServerVersion}}' 2>/dev/null || echo 'unknown'))"
else
  echo "Docker:  NOT AVAILABLE — docker sandbox feature will not work"
  echo "  Make sure the Docker daemon is running on the host."
fi

# 13. Agent image status (do not auto-build — it takes too long).
echo ""
echo "--- Agent image status ---"
for tag in openclaw opencode; do
  if docker image inspect "witty-agent-server:${tag}" &>/dev/null 2>&1; then
    echo "  witty-agent-server:${tag} — present"
  else
    echo "  witty-agent-server:${tag} — MISSING"
    echo "    Build:  docker build --target ${tag} -t witty-agent-server:${tag} ."
  fi
done

echo ""
echo "=== Witty Service devcontainer ready ==="
echo "Run:  uv run uvicorn witty_service.main:create_app --factory --host 0.0.0.0 --port 8000 --reload"
echo "Or press F5 to start debugging."
