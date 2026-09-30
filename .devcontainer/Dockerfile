# -------------------- Stage 0: Node.js 22 运行时 --------------------
FROM node:22.23.3-slim AS node

# -------------------- Stage 1: 开发基础镜像 --------------------
FROM python:3.11-slim AS dev

ARG NPM_REGISTRY=https://registry.npmmirror.com
ARG UV_VERSION=0.8.8
ARG APT_MIRROR=http://mirrors.aliyun.com

# 复用生产镜像的 Node 工具链
COPY --from=node /usr/local/ /usr/local/

# 换用国内 apt 源（默认的 deb.debian.org 在国内慢且易断流）。
# 用 http 是因为 slim 镜像没有 CA 证书，https 会在装 ca-certificates 前握手失败；apt 包本身有 GPG 校验。
RUN set -eux; \
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources; do \
        [ -f "$f" ] || continue; \
        sed -i \
            -e "s|https\?://deb\.debian\.org/debian|${APT_MIRROR}/debian|g" \
            -e "s|https\?://security\.debian\.org/debian-security|${APT_MIRROR}/debian-security|g" \
            "$f"; \
    done

# 系统依赖：git、curl、sudo，以及调试用的 jq/less/procps 和 docker CLI。
# locales 是给 VS Code 内置 Git 扩展用的：它对每个 git 子进程强制注入
# LC_ALL/LANG=en_US.UTF-8（且优先级高于 process.env，容器侧无法覆盖），
# 镜像里若没有该 locale，仓库钩子（#!/bin/bash）启动时会打印 setlocale 警告。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git curl ca-certificates sudo \
        jq less procps \
        docker-cli docker-buildx \
        locales \
    && echo "en_US.UTF-8 UTF-8" > /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*

# uv 走阿里云 PyPI 源，并开启重试/超时以减少断连
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5
RUN pip install --no-cache-dir uv==${UV_VERSION}

# pre-commit（Git 提交钩子）。只在此预装工具本体，**不执行 `pre-commit install`**：
# 后者会覆写 bind-mount 进容器的宿主机 .git/hooks，把容器内的解释器路径写进宿主机仓库；
RUN set -eux; \
    pip_install() { pip install --no-cache-dir -i "$1" pre-commit; }; \
    if ! pip_install "${PIP_INDEX_URL}"; then \
        echo "[devcontainer] WARN: ${PIP_INDEX_URL} unreachable, retrying with https://pypi.org/simple"; \
        pip_install "https://pypi.org/simple" || \
            echo "[devcontainer] WARN: pre-commit 预装失败，post-create.sh 会重试"; \
    fi; \
    command -v pre-commit >/dev/null 2>&1 && pre-commit --version || true

# 开发用户 uid 1000，与 agent 容器的 witty 用户对齐，保证工作区文件互可写
# （devcontainer.json 的 updateRemoteUserUID 会按宿主 UID 重映射）
RUN useradd -m -u 1000 -s /bin/bash vscode \
    && echo "vscode ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/vscode \
    && chmod 0440 /etc/sudoers.d/vscode

# 保证 vscode 用户可以进入 /root 等父目录
RUN chmod a+x /root 2>/dev/null || true

# 全局 npm CLI（版本对齐生产 Dockerfile），registry 与重试配置写入全局 npmrc
RUN npm config set registry "${NPM_REGISTRY}" --location=global \
    && npm config set fetch-retries 5 --location=global \
    && npm config set fetch-retry-maxtimeout 120000 --location=global \
    && npm config set fetch-timeout 300000 --location=global \
    && npm install -g wittyhub openclaw@2026.6.5 \
        opencode-ai@1.17.20 @ai-sdk/openai-compatible @ai-sdk/anthropic \
    && npm cache clean --force

ENV UV_CACHE_DIR=/home/vscode/.cache/uv \
    UV_PROJECT_ENVIRONMENT=/home/vscode/.venv \
    UV_PYTHON_PREFERENCE=system \
    UV_HTTP_TIMEOUT=120 \
    UV_CONCURRENT_DOWNLOADS=16 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# venv 必须放在 witty-home 卷（/home/vscode）内，不能放工作区
RUN runuser -u vscode -- uv venv /home/vscode/.venv \
    && runuser -u vscode -- mkdir -p "$UV_CACHE_DIR" \
    && chown -R vscode:vscode /home/vscode/.venv /home/vscode/.cache

# docker-outside-of-docker 的非标准 socket 路径
ENV DOCKER_HOST=unix:///var/run/docker-host/docker.sock

EXPOSE 8000 5678
