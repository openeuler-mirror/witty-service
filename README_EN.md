# Witty-Service

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) [![PyPI version](https://img.shields.io/pypi/v/witty-service.svg)](https://pypi.org/project/witty-service/) [![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)

As an AI agent lifecycle management service, Witty-Service provides core capabilities such as agent creation, sandbox execution, session management, and message interaction. By exposing a unified RESTful API, it abstracts the differences between underlying sandboxes (Docker/local process/E2B) and runtime adapters (OpenClaw/OpenCode), enabling you to orchestrate and manage AI agents in a unified manner.

## Contents

- [Witty-Service](#witty-service)
  - [Contents](#contents)
  - [Project Introduction](#project-introduction)
    - [Features](#features)
    - [Technical Architecture](#technical-architecture)
    - [Architecture](#architecture)
  - [Installation Guide](#installation-guide)
    - [Method 1: Installation via pip](#method-1-installation-via-pip)
    - [Method 2: Installation from Source](#method-2-installation-from-source)
  - [Quick Start](#quick-start)
  - [Configurations](#configurations)
    - [Core Configurations](#core-configurations)
    - [Docker Sandbox Configurations](#docker-sandbox-configurations)
  - [Deployment Workflow](#deployment-workflow)
    - [Test Environment](#test-environment)
    - [Production Environment](#production-environment)
    - [Boot Parameters](#boot-parameters)
    - [Production Considerations](#production-considerations)
  - [Local Development](#local-development)
    - [Environment Setup](#environment-setup)
    - [Project Structure](#project-structure)
    - [Common Development Commands](#common-development-commands)
    - [Contribution](#contribution)
    - [Development Standards](#development-standards)
  - [License](#license)
  - [Support \& Feedback](#support--feedback)

---

## Project Introduction

Witty-Service is a backend service purpose-built for AI agent scenarios. Its core responsibility is to bundle agent lifecycle management, sandbox-isolated execution, and session/message interactions into a unified architecture, exposing clean RESTful APIs to external clients. Serving as a bridge between upper-layer applications (such as PolyMind) and underlying agent runtimes, it masks the frontend from the operational complexities of whether an agent is running inside a Docker container, a local process, or a cloud-based sandbox.

Typical application scenarios:

- **AI coding assistants**: Provisions agents within isolated sandboxes for each user to securely execute tasks like code generation and file system operations.
- **Multi-LLM chat services**: Provides unified configuration management for diverse LLM providers (including OpenAI, Anthropic, and DeepSeek), enabling seamless, on-demand model switching.
- **Agent skill marketplace**: Dynamically injects specialized capabilities (such as CVE analysis) into agents via a dedicated skill repository.
- **Enterprise-grade agent orchestration**:  Supports production-ready operations and maintenance capabilities, including cron tasks, session pause/resume, and runtime backups.

### Features

- 🤖 **AI agent lifecycle management**: Supports creating, pausing, resuming, and deleting agents, alongside runtime backup and recovery capabilities.
- 📦 **Multi-sandbox isolation**: Supports three sandbox types—Docker, local process, and E2B—allowing you to choose the isolation level on demand.
- 💬 **Session and message management**: Provides multi-session management, supporting both REST (non-streaming) and Server-Sent Events (SSE streaming) message interaction modes.
- 🔌 **Multi-runtime adaptation**: Plugs into different agent runtimes, such as OpenClaw and OpenCode, via a dedicated adapter layer.
- 🧠 **Multi-model management**: Offers unified configuration for 10+ model providers, including OpenAI, Anthropic, Google, DeepSeek, GLM, and Kimi.
- 🛠️ **Skill marketplace**: Features built-in skill repository synchronization, supporting custom skill package uploads and installation from the marketplace.
- 🔐 **Secure authentication**: Implements a Bearer Token-based API authentication mechanism.
- 📊 **CVE and backporting services**: Built-in CVE analysis and backporting services.

### Technical Architecture

| Layer | Technology Stack |
|------|--------|
| Web framework | FastAPI |
| ASGI server | Uvicorn |
| Database | SQLAlchemy + Alembic (migrations) |
| Communication protocols | WebSocket + REST + SSE |
| Sandbox management | Docker SDK/subprocess/E2B SDK |
| Package management | uv |
| Language | Python 3.11+ |

### Architecture

```text
                              ┌─────────────────────────────────┐
                              │ Upper-layer app (like PolyMind) │
                              └───────────────┬─────────────────┘
                                              │
                                     RESTful API / SSE
                                              │
                                              ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                                   Witty-Service                                     │
│                                                                                     │
│         ┌─ API Layer ───────────────────────────────────────────────────┐           |
│         │  /agents  ·  /models  ·  /skills  ·  /cve  ·  /backport       │           │
│         │  Auth (Bearer Token)  ·  Error Handler  ·  Schemas            │           │
│         └────────────────────────────┬──────────────────────────────────┘           │
│                                      │                                              │
│         ┌─ Application Layer ────────▼──────────────────────────────────┐           │
│         │  AgentManager · SessionManager · SkillManager                 │           │
│         │  CVEService  · BackportService                                │           │
│         └──┬───────────────────────────────┬───────────────────────┬────┘           │
│            │                               │                       │                │
│  ┌─────────▼─────────────────┐    ┌────────▼──────────┐    ┌───────▼──────────┐     │
│  │ Adapter Layer             │    │ Persistence Layer │    │  Storage Layer   │     │
│  │ WebSocket client          │    │ SQLAlchemy ORM    │    │  WorkspaceStore  │     │
│  │ HTTP client               │    │ SQLite+Alembic    │    │  RuntimeBackup   │     │
│  │ Connection pool/Protocols │    │ Repository        │    │                  │     │
│  └──┬────────────────────────┘    └───────────────────┘    └──────────────────┘     │
│     │                                                                               │
│  ┌──▼────────────────────────────────────────────────────────────────────────────┐  │
│  │  Sandbox layer                                                                │  │
│  │  ┌──────────┐  ┌──────────────┐  ┌──────────┐                                 │  │
│  │  │  Docker  │  │Local Process │  │   E2B    │                                 │  │
│  │  └────┬─────┘  └──────┬───────┘  └────┬─────┘                                 │  │
│  │       └────────┬──────┘               │                                       │  │
│  │                │                      │                                       │  │
│  │       AdapterEndpoint                 │                                       │  │
│  └────────────────┼──────────────────────┼───────────────────────────────────────┘  │
│                   │                      │                                          │
│         ┌─────────▼─────────────┐  ┌─────▼────────────┐                             │
│         │ Domain (Enums/Errors) │  │   Config         │                             │
│         └────────┬──────────────┘  └──────────────┬───┘                             │
└──────────────────┼────────────────────────────────┼─────────────────────────────────┘
                   │                                │
         ┌─────────▼────────────────────┐  ┌────────▼─────────┐
         │           HTTP REST          │  │     HTTP REST    │
         │ (Lifecycle/Skill management) │  │ (E2B Cloud API)  │
         └─────────┬────────────────────┘  └──────────────────┘
                   │
         ┌─────────▼───────────────────────┐
         │            WebSocket            │
         │ (Streaming messages/Event push) │
         └─────────┬───────────────────────┘
                   │
                   |
┌──────────────────▼───────────────────────────────────────────────────┐
│                       Witty-Agent-Server                             │
│                                                                      │
│  ┌─ API Layer ───────────────────────────────────────────────────┐   │
│  │  AgentRouter  ·  SessionRouter  ·  SessionWSRouter            │   │
│  └──────────────────────────┬────────────────────────────────────┘   │
│                             │                                        │
│  ┌─ Application Layer ──────▼────────────────────────────────────┐   │
│  │  AgentService  ·  SessionService  ·  SkillService             │   │
│  │  SessionWSOrchestrator  ·  TaskPool                           │   │
│  │  Materialization                                              │   │
│  └──────────────────────────┬────────────────────────────────────┘   │
│                             │                                        │
│  ┌─ Runtime Layer ──────────▼────────────────────────────────────┐   │
│  │  RuntimeBase (ABC)                                            │   │
│  │  ├─ OpenClawGatewayRuntime                                    │   │
│  │  └─ OpenCodeRuntime (WIP)                                     │   │
│  └───────────────────────────┬────────────────────────────────────┘  │
│                              │                                       │
│  ┌─ Adapter Layer ───────────▼──────────────┐  ┌─ Infra Layer ───┐   │
│  │ OpenClawAdapter  ·  RuntimeRegistry      │  │  GatewayClient  │   │
│  └──────────────────────────────────────────┘  │  (WS RPC)       │   │
│                                                └─────────┬───────┘   │
└──────────────────────────────────────────────────────────┼───────────┘
                                                           │
                                                     WebSocket RPC
                                                           │
                                                           │
                                           ┌───────────────▼──────────┐
                                           │    OpenClaw Gateway      │
                                           │ (Agent Runtime Platform) │
                                           └──────────────────────────┘
```

---

## Installation Guide

### Method 1: Installation via pip

If you only need to run Witty-Service without participating in development, install it directly via pip.

```bash
pip install witty-service
```

Once the installation is complete, start the service using the CLI.

```bash
witty-service --host 0.0.0.0 --port 8000
```

> **Prerequisites**: Python 3.11 or later

### Method 2: Installation from Source

If you want to contribute to development or perform a custom build, install from source.

**Prerequisites**

| Dependency | Description | Installation Method |
|------|------|----------|
| Python | 3.11+ | [python.org](https://www.python.org/downloads/) |
| uv | Python package manager | [docs.astral.sh/uv](https://docs.astral.sh/uv/) |
| Docker | Sandbox runtime (optional) | [docker.com](https://www.docker.com/) |

**Procedure**

1. Clone the repository.

    ```bash
    git clone https://gitcode.com/openeuler/witty-service.git
    cd witty-service
    ```

2. Create a virtual environment and install dependencies.

    ```bash
    uv venv
    source .venv/bin/activate
    uv pip install -e ".[dev]"
    ```

3. Verify the installation.

    ```bash
    witty-service --help
    ```

---

## Quick Start

**1. Start the service.**

```bash
witty-service --host 0.0.0.0 --port 8000
```

**2. Verify that the service is running properly.**

```bash
curl http://127.0.0.1:8000/healthz
```

Expected response:

```json
{"status": "ok"}
```

> **Note**: The default authentication token is `dev-token`. For production environments, modify it using the `AUTH_TOKEN` environment variable.

---

## Configurations

Witty-Service is configured via environment variables, eliminating the need for additional configuration files.

### Core Configurations

| Environment Variable | Description | Default Value |
|----------|------|--------|
| `AUTH_TOKEN` | API authentication token | `dev-token` |
| `WITTY_AGENT_SERVER_APP_DIR` | The code directory of `witty-agent-server` in local process mode | - |

### Docker Sandbox Configurations

| Environment Variable | Description | Default Value |
|----------|------|--------|
| `WITTY_DOCKER_HOST` | Docker service listening address | `127.0.0.1` |
| `WITTY_DOCKER_IMAGE` | Image name (excluding tag) | `witty-agent-server` |
| `WITTY_DOCKER_IMAGE_TAG` | Image tag | `latest` |
| `WITTY_DOCKER_CONTAINER_PORT` | Service port inside the container | `8080` |
| `WITTY_DOCKER_CONTAINER_WORKSPACE_PATH` | Workspace path inside the container | `/witty-workspace` |
| `WITTY_DOCKER_STOP_TIMEOUT` | Container stop timeout (in seconds) | `10` |

---

## Deployment Workflow

### Test Environment

Ideal for integration testing and functional verification:

```bash
# Build the pip package.
uv build

# Install the build artifacts.
uv pip install dist/witty_service-0.1.0-py3-none-any.whl

# Start the service.
witty-service --host 0.0.0.0 --port 8000
```

Run tests:

```bash
# Unit tests
uv run pytest tests/unit/ -q

# E2E tests
uv run pytest tests/e2e/ -q

# Full tests
uv run pytest tests/ -q
```

### Production Environment

**(Recommended) Method 1: Installation via pip**

```bash
pip install witty-service

# Multi-worker startup
witty-service --host 0.0.0.0 --port 8000 --workers 4
```

**Method 2: Build from Source**

```bash
git clone https://gitcode.com/openeuler/witty-service.git
cd witty-service
uv build
uv pip install dist/witty_service-0.1.0-py3-none-any.whl

witty-service --host 0.0.0.0 --port 8000 --workers 4
```

### Boot Parameters

| Parameter | Description | Default Value |
|------|------|--------|
| `--host` | Host address to bind | `0.0.0.0` |
| `--port` | Port to bind | `8000` |
| `--log-level` | Log level (debug/info/warning/error/critical) | `info` |
| `--reload` | Auto-reload for development mode | `False` |
| `--workers` | Number of worker processes | `1` |

### Production Considerations

- **Authentication token**: Always change the default token using the `AUTH_TOKEN` environment variable. Use a cryptographically strong random string.
- **Worker count**: Set `--workers` appropriately based on the available CPU cores.
- **Reverse proxy**: It is recommended to deploy a reverse proxy like Nginx in front of Witty-Service to handle HTTPS certification termination.
- **Process management**: Use process managers such as `systemd` or Supervisor to manage service processes and enable automatic restarts.
- **Database migration**: Always run `alembic upgrade head` to apply pending database migrations before deploying a new version.

---

## Local Development

### Environment Setup

```bash
# 1. Clone the repository.
git clone https://gitcode.com/openeuler/witty-service.git
cd witty-service

# 2. Create a virtual environment and install dependencies.
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# 3. Initialize the database.
alembic upgrade head

# 4. Start the development server.
uv run uvicorn src.witty_service.main:create_app --factory --host 0.0.0.0 --port 8000 --reload
```

### Project Structure

```text
witty-service/
├── src/
│   ├── witty_service/                # Main service package
│   │   ├── main.py                   # FastAPI application entry point
│   │   ├── cli.py                    # CLI entry point
│   │   ├── config.py                 # Configuration management
│   │   ├── api/                      # API routing layer
│   │   │   ├── agents.py             # Agent-related endpoints
│   │   │   ├── models.py             # Model configuration endpoints
│   │   │   ├── skills.py             # Skill management endpoints
│   │   │   ├── cve.py                # CVE endpoints
│   │   │   ├── backport.py           # Code backporting endpoints
│   │   │   ├── auth.py               # Authentication middleware
│   │   │   ├── errors.py             # Unified error handling
│   │   │   └── schemas.py            # Request/Response validation schemas
│   │   ├── application/              # Business logic layer
│   │   │   ├── agent_manager.py      # Agent lifecycle management
│   │   │   ├── session_manager.py    # Session management
│   │   │   └── skill_manager.py      # Skill management
│   │   ├── adapter/                  # Adapter layer (communicating with witty-agent-server)
│   │   │   ├── websocket_client.py   # WebSocket client
│   │   │   ├── websocket_protocol.py # WebSocket protocol definitions
│   │   │   └── http_client.py        # HTTP client
│   │   ├── sandbox/                  # Sandbox isolation layer
│   │   │   ├── base.py               # Base sandbox class
│   │   │   ├── docker.py             # Docker sandbox implementation
│   │   │   ├── local_process.py      # Local process sandbox implementation
│   │   │   ├── e2b.py                # E2B cloud sandbox implementation
│   │   │   └── factory.py            # Sandbox factory pattern
│   │   ├── domain/                   # Domain models
│   │   ├── persistence/              # Data persistence layer
│   │   └── storage/                  # File storage management
│   └── witty_agent_server/           # Agent runtime service
│       ├── app.py                    # FastAPI application instance
│       ├── api/routers/              # API routes
│       ├── application/services/     # Business services
│       │   ├── agent/                # Agent runtime services
│       │   ├── session/              # Session handling services
│       │   └── skill/                # Skill execution services
│       ├── runtimes/                 # Runtime implementations
│       ├── adapters/                 # Runtime adapters
│       └── infra/                    # Infrastructure & shared utilities
├── tests/
│   ├── unit/                         # Unit tests
│   └── e2e/                          # E2E tests
├── alembic/                          # Database migrations
├── docs/                             # Documentation
├── pyproject.toml                    # Project configurations
└── .github/workflows/                # CI/CD workflows
```

### Common Development Commands

| Command | Description |
|------|------|
| `uv run uvicorn src.witty_service.main:create_app --factory --reload` | Starts the development server with hot-reload enabled. |
| `uv run pytest tests/unit/ -q` | Runs unit tests. |
| `uv run pytest tests/e2e/ -q` | Runs E2E tests. |
| `uv run pytest tests/ -q` | Runs the full test suite. |
| `uv build` | Builds the project into a pip package. |
| `alembic revision --autogenerate -m "description"` | Generates a database migration script. |
| `alembic upgrade head` | Applies pending migrations. |

### Contribution

1. Fork 本仓库
2. Create a feature branch: `git checkout -b feature/your-feature`
3. Commit your changes: `git commit -m 'feat: add your feature'`
4. Push the branch: `git push origin feature/your-feature`
5. Create a pull request (PR).

### Development Standards

- **Code style**: Use Ruff (lint + format, `line-length=88`) as the replacement for Black/Flake8; see `[tool.ruff]` in `pyproject.toml`.
- **Type checking**: Use `mypy` for static type checking configured in `strict` mode.
- **Commit conventions**: Follow semantic commit messaging (for example, `feat:`, `fix:`, `docs:`, and `refactor:`); the pre-commit gitlint hook validates the format.
- **Test coverage**: Any new features must include corresponding unit tests. Ensure all tests pass successfully before submission.
- **Database migrations**: Whenever domain models are modified, a corresponding Alembic migration script must be generated.

### Code Checks (pre-commit)

1. Install pre-commit: `uv tool install pre-commit`
2. Install the git hooks: `pre-commit install --hook-type pre-commit --hook-type commit-msg` (or keep the repo convention: `cp .githooks/pre-commit .git/hooks/pre-commit`, then run `pre-commit install --hook-type commit-msg` to enable gitlint commit-message validation)
3. Staged files are checked automatically on commit; **for a full check, first clean up existing findings**: `uv run ruff check --fix . && uv run ruff format .`, then run `pre-commit run --all-files`
4. Checks include: Ruff (lint/format), Bandit (security), Codespell (spelling), pre-commit-hooks (basic formatting and secrets), markdownlint, ShellCheck (when installed locally), and gitlint (commit messages); Gitleaks (deep secret scan) can be enabled once network access to go.dev/GitHub is available

---

## License

This project is open-source and licensed under the [MIT License](LICENSE).

## Support & Feedback

- **Issue tracking**: Please submit bug reports and feedback via [GitCode Issues](https://gitcode.com/openeuler/witty-service/issues).
- **Feature proposals**: Contributions and feature suggestions are welcome through issues or PRs.
- **Project homepage**: [https://gitcode.com/openeuler/witty-service](https://gitcode.com/openeuler/witty-service)
