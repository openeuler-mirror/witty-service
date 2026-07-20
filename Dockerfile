# -------------------- Stage 0: Node.js 依赖层 --------------------
FROM node:22.22.2-slim AS node

# -------------------- Stage 1: 基础层（apt 依赖，不含 runtime CLI）--------------------
FROM python:3.11-slim AS base

COPY --from=node /usr/local/ /usr/local/

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git \
    && rm -rf /var/lib/apt/lists/*

# -------------------- Stage 2: Python 依赖层 --------------------
FROM base AS python-deps

WORKDIR /app

RUN pip install --no-cache-dir --no-compile \
    fastapi>=0.115 \
    uvicorn>=0.20 \
    websockets>=15.0.1 \
    httpx>=0.27 \
    pyyaml>=6.0 \
    cryptography>=42.0.0

COPY src/witty_agent_server/ ./witty_agent_server/
COPY src/witty_service/__init__.py ./witty_service/__init__.py
COPY src/witty_service/config.py ./witty_service/config.py
COPY src/witty_service/domain/ ./witty_service/domain/

# -------------------- Stage 3: 最终基础层（用户、权限、HEALTHCHECK、CMD 共享）--------------------
FROM python-deps AS final-base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1

# 安装 wittyhub（供 openclaw/opencode skill install/uninstall 使用）
RUN npm install -g wittyhub \
    && npm cache clean --force

RUN useradd -u 1000 -m -s /bin/bash witty \
    && chown witty:witty /home/witty \
    && chmod 755 /home/witty \
    && mkdir -p ~/.witty/logs ~/.witty/db \
    && chown -R witty:witty /app ~/.witty \
    && passwd -l witty \
    && rm -rf /etc/sudoers.d/* \
    && find /usr/local/lib/python3.11 -type d -name __pycache__ -exec rm -rf {} + \
    && find /usr/local/lib/python3.11 -type d -name tests -exec rm -rf {} +

USER witty

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8080/ping || exit 1

CMD ["uvicorn", "witty_agent_server.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

# -------------------- Stage 4a: OpenClaw runtime --------------------
FROM final-base AS openclaw

USER root

ENV OPENCLAW_VERSION=2026.6.5

RUN npm install -g openclaw@${OPENCLAW_VERSION} \
    && npm cache clean --force

USER witty

# -------------------- Stage 4b: OpenCode runtime --------------------
FROM final-base AS opencode

USER root

ENV OPENCODE_VERSION=1.17.20

RUN npm install -g opencode-ai@${OPENCODE_VERSION} \
    && npm install -g @ai-sdk/openai-compatible @ai-sdk/anthropic \
    && npm cache clean --force

USER witty
