# -------------------- Stage 0: Node.js 22 runtime --------------------
FROM node:22.22.2-slim AS node

# -------------------- Stage 1: dev base --------------------
FROM python:3.11-slim AS dev

ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG UV_VERSION=0.8.8

# Copy Node toolchain (identical to production Dockerfile pattern)
COPY --from=node /usr/local/ /usr/local/

# System dependencies:
#   git (gitpython), curl (healthcheck), sudo (permission fixes),
#   jq less procps (debugging), docker-cli (docker command, recommended by docker.io),
#   docker-buildx (BuildKit plugin)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl ca-certificates sudo \
        jq less procps \
        docker-cli docker-buildx \
    && rm -rf /var/lib/apt/lists/*

# uv installed from Aliyun mirror (uv.lock already uses mirrors.aliyun.com/pypi/simple/)
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir uv==${UV_VERSION}

# Dev user: uid 1000 aligns with agent containers' 'witty' user (uid 1000).
# This ensures workspace files created by either side are mutually writable.
# NOTE: devcontainer.json sets updateRemoteUserUID=true, which remaps this user
# to the HOST user's UID — the uid-1000 alignment only holds when host UID is 1000.
RUN useradd -m -u 1000 -s /bin/bash vscode \
    && echo "vscode ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/vscode \
    && chmod 0440 /etc/sudoers.d/vscode

# Ensure workspace parent directories are traversable by the dev user.
# The actual workspace path is determined at runtime (${localWorkspaceFolder}),
# but common parents like /root need o+x for the vscode user to cd into them.
RUN chmod a+x /root 2>/dev/null || true

# Global npm CLIs — exact versions matching production Dockerfile
RUN npm config set registry "${NPM_REGISTRY}" --location=global \
    && npm install -g wittyhub openclaw@2026.6.5 \
        opencode-ai@1.17.20 @ai-sdk/openai-compatible @ai-sdk/anthropic \
    && npm cache clean --force

ENV UV_CACHE_DIR=/home/vscode/.cache/uv \
    UV_PYTHON_PREFERENCE=system \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# docker-outside-of-docker: non-standard socket path (official feature convention)
ENV DOCKER_HOST=unix:///var/run/docker-host/docker.sock

EXPOSE 8000 5678
