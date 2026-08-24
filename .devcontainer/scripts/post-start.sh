#!/usr/bin/env bash
# Lightweight diagnostics on every container start.
set -euo pipefail
# Derive workspace folder from script path.
# Script is at .devcontainer/scripts/post-start.sh — go up 3 levels to workspace root.
# Invoked with absolute path from devcontainer.json (containerWorkspaceFolder variable).
WORKSPACE_DIR="$(dirname "$(dirname "$(dirname "$0")")")"
cd "$WORKSPACE_DIR"

echo "--- Witty Service devcontainer status ---"
echo "Python:  $(python --version 2>&1 || echo 'NOT FOUND')"
echo "Node:    $(node --version 2>&1 || echo 'NOT FOUND')"
echo "uv:      $(uv --version 2>&1 || echo 'NOT FOUND')"

# Docker socket availability and permissions.
if [ -S /var/run/docker-host/docker.sock ]; then
  # Keep the vscode user in the socket's host group (idempotent; see post-create.sh).
  SOCK_GID="$(stat -c '%g' /var/run/docker-host/docker.sock 2>/dev/null || echo '')"
  if [ -n "$SOCK_GID" ]; then
    if ! getent group "$SOCK_GID" >/dev/null 2>&1; then
      sudo groupadd -g "$SOCK_GID" docker-host 2>/dev/null || true
    fi
    DOCKER_GROUP="$(getent group "$SOCK_GID" | cut -d: -f1 2>/dev/null || echo docker-host)"
    sudo usermod -aG "$DOCKER_GROUP" vscode 2>/dev/null || true
  fi
  if docker info &>/dev/null 2>&1; then
    echo "Docker:  reachable"
  else
    echo "Docker:  socket present but daemon unreachable — is the host Docker daemon running?"
  fi
else
  echo "Docker:  socket NOT FOUND — docker sandbox will not work"
fi

# Key file checks.
if [ -f .env ]; then
  echo ".env: present"
else
  echo ".env: MISSING — copy .env.example to .env"
fi

if [ -d .venv ]; then
  echo ".venv: present"
else
  echo ".venv: missing — run post-create setup or 'uv sync --extra dev'"
fi

if [ -d agent-workspaces ]; then
  echo "agent-workspaces: present"
else
  echo "agent-workspaces: missing"
fi

# Ensure agent-workspaces is writable by uid 1000 (agent containers' witty user).
sudo chown -R 1000:1000 agent-workspaces 2>/dev/null || true

# Configure git safe directory for the workspace.
git config --global --add safe.directory "$(pwd)" 2>/dev/null || true

echo "Ready. Run 'uv run pytest tests/unit/ -q' to verify."
