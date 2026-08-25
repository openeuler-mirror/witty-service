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
#    Run directly as root (lifecycle commands default) — faster and avoids /root traversal issues.
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
# Ensure vscode user owns .venv for subsequent operations from the terminal
chown -R vscode:vscode .venv 2>/dev/null || true
echo "[ok] Python dependencies installed"

# 7. Ensure runtime directories exist (used by witty-service for DB, logs, etc.).
#    The witty-home volume is at /home/vscode — create dirs and fix ownership.
echo "[...] Creating runtime directories..."
mkdir -p /home/vscode/.witty/db /home/vscode/.witty/logs
chown -R vscode:vscode /home/vscode/.witty
echo "[ok] Runtime directories ready"

# 8. Initialize the database with Alembic migrations.
#    Use absolute DATABASE_URL (sqlite://// = 4 slashes = absolute path) to avoid
#    ~ expansion ambiguity since this script runs as root.
echo "[...] Running Alembic migrations..."
WITTY_DATABASE_URL="sqlite:////home/vscode/.witty/db/witty_service.sqlite3" \
    uv run alembic upgrade head 2>/dev/null || {
  echo "[warn] Alembic migration failed — DB will be auto-created on first run"
}
echo "[ok] Database initialized"

# 9. Git safe.directory for the workspace (both root and vscode).
git config --global --add safe.directory "$(pwd)" 2>/dev/null || true
runuser -u vscode -- git config --global --add safe.directory "$(pwd)" 2>/dev/null || true

# 10. Print toolchain versions for parity check with CI/Dockerfile.
echo "--- Toolchain versions ---"
echo "Python:  $(python --version)"
echo "Node:    $(node --version)"
echo "npm:     $(npm --version)"
echo "uv:      $(uv --version)"
echo "openclaw: $(openclaw --version 2>&1 | head -1 || echo 'not found')"

# 11. Docker availability check.
if docker info &>/dev/null; then
  echo "Docker:  available ($(docker info --format '{{.ServerVersion}}' 2>/dev/null || echo 'unknown'))"
else
  echo "Docker:  NOT AVAILABLE — docker sandbox feature will not work"
  echo "  Make sure the Docker daemon is running on the host."
fi

# 12. Agent image status (do not auto-build — it takes too long).
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
