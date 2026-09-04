"""
Witty Service 统一配置管理

所有环境变量集中在此模块管理，支持的配置项详见下方各配置类。

环境变量列表:
    # 基础配置
    AUTH_TOKEN                      认证令牌 (默认: dev-token)

    # 工作空间配置
    WITTY_WORKSPACE_ROOT           工作空间根目录 (默认: ~/.witty)
    WITTY_AGENT_SERVER_APP_DIR     Agent服务器应用目录 (可选)
    PATCHFLOW_STATE_ROOT           Patchflow 锁与日志根目录 (默认: ~/.patchflow)

    # 数据库配置
    WITTY_DATABASE_URL             数据库连接URL (默认: sqlite:///~/.witty/db/witty_service.sqlite3)
    WITTY_DATABASE_AUTO_CREATE     是否允许启动时自动建表 (默认: true)

    # 日志配置
    WITTY_LOG_LEVEL                日志级别: DEBUG, INFO, WARNING, ERROR (默认: INFO)
    WITTY_LOG_FILE                 日志文件路径 (默认: WITTY_WORKSPACE_ROOT/logs/witty-service.log)
    WITTY_LOG_MAX_BYTES            单个日志文件最大字节数 (默认: 10485760)
    WITTY_LOG_BACKUP_COUNT         备份日志文件数量 (默认: 5)

    # Docker沙箱配置
    WITTY_DOCKER_HOST              Docker主机地址 (默认: 127.0.0.1)
    WITTY_DOCKER_CONTAINER_PORT    Docker容器端口 (默认: 8080)
    WITTY_DOCKER_CONTAINER_WORKSPACE_PATH  Docker容器内工作空间路径 (默认: /witty-workspace)
    WITTY_DOCKER_STOP_TIMEOUT      Docker容器停止超时时间(秒) (默认: 10)
    WITTY_DOCKER_IMAGE             Docker镜像名称 (默认: ghcr.io/openwitty/witty-agent-server)

    # OpenClaw Gateway配置
    OPENCLAW_GATEWAY_IDLE_TIMEOUT            网关空闲超时时间(秒) (默认: 1200)
    OPENCLAW_GATEWAY_LIFECYCLE_END_DRAIN_TIMEOUT  生命周期结束排水超时时间(秒) (默认: 60)
    OPENCLAW_STATE_DIR               状态目录 (可选)

    # OpenClaw配置
    OPENCLAW_CONFIG_PATH            OpenClaw配置文件路径 (默认: ~/.openclaw/openclaw.json)

    # Witty Insight 配置
    WITTY_INSIGHT_ENABLED          是否启用 insight 集成 (默认: true)
    WITTY_INSIGHT_BASE_URL         insight API 地址 (默认: http://127.0.0.1:7396)
    WITTY_INSIGHT_TIMEOUT_SECONDS  insight 请求超时(秒) (默认: 10)
    WITTY_INSIGHT_BEARER_TOKEN     insight Bearer Token (可选)

使用示例:
    from witty_service.config import get_settings

    settings = get_settings()
    print(settings.database.url)
    print(settings.docker.host)
    print(settings.logging.level)
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ==============================================================================
# CORS 配置
# ==============================================================================


@dataclass(frozen=True)
class CorsSettings:
    """CORS 跨域资源共享配置"""

    origins: list[str] = field(default_factory=lambda: ["*"])
    credentials: bool = True
    methods: list[str] = field(
        default_factory=lambda: ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
    )
    headers: list[str] = field(default_factory=lambda: ["*"])


# ==============================================================================
# 数据库配置
# ==============================================================================


@dataclass(frozen=True)
class DatabaseSettings:
    """数据库连接配置

    环境变量:
        WITTY_DATABASE_URL: 数据库连接URL
            - SQLite: sqlite:///path/to/db.sqlite3
            - PostgreSQL: postgresql://user:pass@localhost/dbname
            - 默认: sqlite:///~/.witty/db/witty_service.sqlite3
        WITTY_DATABASE_AUTO_CREATE: 是否允许使用 ORM 自动建表
            - true/false
            - 默认: true
    """

    url: str
    auto_create: bool = True

    @classmethod
    def from_env(cls) -> "DatabaseSettings":
        default_db_path = Path("~/.witty/db/witty_service.sqlite3").expanduser()
        url = os.getenv("WITTY_DATABASE_URL", f"sqlite:///{default_db_path}")
        auto_create = os.getenv("WITTY_DATABASE_AUTO_CREATE", "true").lower() in (
            "1",
            "true",
            "yes",
        )
        return cls(url=url, auto_create=auto_create)


# ==============================================================================
# 日志配置
# ==============================================================================


@dataclass(frozen=True)
class LoggingSettings:
    """日志系统配置

    环境变量:
        WITTY_LOG_LEVEL: 日志级别 (DEBUG, INFO, WARNING, ERROR), 默认 INFO
        WITTY_LOG_FILE: 日志文件路径, 默认 WITTY_WORKSPACE_ROOT/logs/witty-service.log
        WITTY_LOG_MAX_BYTES: 单个日志文件最大字节数, 默认 10MB
        WITTY_LOG_BACKUP_COUNT: 备份日志文件数量, 默认 5
    """

    level: str = "INFO"
    file: str = ""
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5

    @classmethod
    def from_env(cls) -> "LoggingSettings":
        workspace_root = Path(
            os.getenv("WITTY_WORKSPACE_ROOT", "~/.witty")
        ).expanduser()
        default_log_file = workspace_root / "logs" / "witty-service.log"
        return cls(
            level=os.getenv("WITTY_LOG_LEVEL", "INFO").upper(),
            file=os.getenv("WITTY_LOG_FILE", str(default_log_file)),
            max_bytes=int(os.getenv("WITTY_LOG_MAX_BYTES", 10 * 1024 * 1024)),
            backup_count=int(os.getenv("WITTY_LOG_BACKUP_COUNT", 5)),
        )


# ==============================================================================
# Docker 沙箱配置
# ==============================================================================


@dataclass(frozen=True)
class DockerSettings:
    """Docker 沙箱运行时配置

    环境变量:
        WITTY_DOCKER_HOST: Docker 守护进程主机地址, 默认 127.0.0.1
        WITTY_DOCKER_CONTAINER_PORT: 容器内部服务端口, 默认 8080
        WITTY_DOCKER_CONTAINER_WORKSPACE_PATH: 容器内工作空间挂载路径, 默认 /witty-workspace
        WITTY_DOCKER_STOP_TIMEOUT: 容器停止超时时间(秒), 默认 10
        WITTY_DOCKER_IMAGE: Docker 镜像名称, 默认 ghcr.io/openwitty/witty-agent-server

        # 资源管控
        WITTY_DOCKER_MEMORY_LIMIT: 容器内存硬限制, 默认 512m
        WITTY_DOCKER_PIDS_LIMIT: 容器内最大进程数, 默认 100
        WITTY_DOCKER_CPU_SHARES: CPU 相对权重, 默认 512
        WITTY_DOCKER_NOFILE_SOFT_LIMIT: 文件描述符软限制, 默认 1024
        WITTY_DOCKER_NOFILE_HARD_LIMIT: 文件描述符硬限制, 默认 4096
        WITTY_DOCKER_TMPFS_SIZE: /tmp tmpfs 大小, 默认 256M
        WITTY_DOCKER_READ_ONLY: 根文件系统只读, 默认 true
    """

    host: str = "127.0.0.1"
    container_port: int = 8080
    container_workspace_path: str = "/witty-workspace"
    stop_timeout: int = 10
    image: str = "ghcr.io/openwitty/witty-agent-server"
    memory_limit: str = "512m"
    pids_limit: int = 100
    cpu_shares: int = 512
    nofile_soft_limit: int = 1024
    nofile_hard_limit: int = 4096
    tmpfs_size: str = "256M"
    read_only: bool = True

    @classmethod
    def from_env(cls) -> "DockerSettings":
        return cls(
            host=os.getenv("WITTY_DOCKER_HOST", "127.0.0.1"),
            container_port=int(os.getenv("WITTY_DOCKER_CONTAINER_PORT", 8080)),
            container_workspace_path=os.getenv(
                "WITTY_DOCKER_CONTAINER_WORKSPACE_PATH", "/witty-workspace"
            ),
            stop_timeout=int(os.getenv("WITTY_DOCKER_STOP_TIMEOUT", 10)),
            image=os.getenv(
                "WITTY_DOCKER_IMAGE", "ghcr.io/openwitty/witty-agent-server"
            ),
            memory_limit=os.getenv("WITTY_DOCKER_MEMORY_LIMIT", "512m"),
            pids_limit=int(os.getenv("WITTY_DOCKER_PIDS_LIMIT", "100")),
            cpu_shares=int(os.getenv("WITTY_DOCKER_CPU_SHARES", "512")),
            nofile_soft_limit=int(os.getenv("WITTY_DOCKER_NOFILE_SOFT_LIMIT", "1024")),
            nofile_hard_limit=int(os.getenv("WITTY_DOCKER_NOFILE_HARD_LIMIT", "4096")),
            tmpfs_size=os.getenv("WITTY_DOCKER_TMPFS_SIZE", "256M"),
            read_only=os.getenv("WITTY_DOCKER_READ_ONLY", "true").lower()
            not in ("0", "false", "no"),
        )


# ==============================================================================
# OpenClaw Gateway 配置
# ==============================================================================


@dataclass(frozen=True)
class OpenClawGatewaySettings:
    """OpenClaw Gateway 网关配置

    环境变量:
        OPENCLAW_GATEWAY_IDLE_TIMEOUT: 网关空闲超时时间(秒), 默认 1200 (20分钟)
        OPENCLAW_GATEWAY_LIFECYCLE_END_DRAIN_TIMEOUT: 生命周期结束排水超时时间(秒), 默认 60
        OPENCLAW_STATE_DIR: 网关状态存储目录, 默认 ~/.openclaw
    """

    idle_timeout: float = 1200.0
    lifecycle_end_drain_timeout: float = 60.0
    state_dir: str | None = None

    @classmethod
    def from_env(cls) -> "OpenClawGatewaySettings":
        return cls(
            idle_timeout=float(os.getenv("OPENCLAW_GATEWAY_IDLE_TIMEOUT", "1200")),
            lifecycle_end_drain_timeout=float(
                os.getenv("OPENCLAW_GATEWAY_LIFECYCLE_END_DRAIN_TIMEOUT", "60")
            ),
            state_dir=os.getenv("OPENCLAW_STATE_DIR"),
        )


# ==============================================================================
# 工作空间配置
# ==============================================================================


@dataclass(frozen=True)
class WorkspaceSettings:
    """工作空间目录配置

    环境变量:
        WITTY_WORKSPACE_ROOT: 工作空间根目录, 默认 ~/.witty
        WITTY_AGENT_SERVER_APP_DIR: Agent 服务器应用目录, 可选
        WITTY_RECOVERY_MAX_CONCURRENT: 启动时恢复 agent 的最大并发数, 默认 5
    """

    root: str = "~/.witty"
    agent_server_app_dir: str | None = None
    recovery_max_concurrent: int = 5

    @classmethod
    def from_env(cls) -> "WorkspaceSettings":
        return cls(
            root=os.getenv("WITTY_WORKSPACE_ROOT", "~/.witty"),
            agent_server_app_dir=os.getenv("WITTY_AGENT_SERVER_APP_DIR"),
            recovery_max_concurrent=int(
                os.getenv("WITTY_RECOVERY_MAX_CONCURRENT", "5")
            ),
        )

    def root_path(self) -> Path:
        """获取工作空间根目录 (Path 对象)"""
        return Path(self.root).expanduser()


# ==============================================================================
# OpenClaw 配置
# ==============================================================================


@dataclass(frozen=True)
class OpenClawSettings:
    """OpenClaw 通用配置

    环境变量:
        OPENCLAW_CONFIG_PATH: OpenClaw 配置文件路径, 默认 ~/.openclaw/openclaw.json
    """

    config_path: str = "~/.openclaw/openclaw.json"

    @classmethod
    def from_env(cls) -> "OpenClawSettings":
        return cls(
            config_path=os.getenv("OPENCLAW_CONFIG_PATH", "~/.openclaw/openclaw.json"),
        )

    def config_path_resolved(self) -> Path:
        """获取配置文件路径 (Path 对象)"""
        return Path(self.config_path).expanduser().resolve(strict=False)


# ==============================================================================
# Witty Insight 配置
# ==============================================================================


@dataclass(frozen=True)
class InsightSettings:
    """Witty Insight 上游服务配置

    环境变量:
        WITTY_INSIGHT_ENABLED: 是否启用 insight 集成, 默认 true
        WITTY_INSIGHT_BASE_URL: insight API 地址, 默认 http://127.0.0.1:7396
        WITTY_INSIGHT_TIMEOUT_SECONDS: insight 请求超时(秒), 默认 10
        WITTY_INSIGHT_BEARER_TOKEN: 调用 insight 时附带的 Bearer Token, 可选
    """

    enabled: bool = True
    base_url: str = "http://127.0.0.1:7396"
    timeout_seconds: float = 10.0
    bearer_token: str | None = None

    @classmethod
    def from_env(cls) -> "InsightSettings":
        raw_token = os.getenv("WITTY_INSIGHT_BEARER_TOKEN")
        bearer_token = raw_token.strip() or None if raw_token is not None else None
        return cls(
            enabled=os.getenv("WITTY_INSIGHT_ENABLED", "true").lower()
            in ("1", "true", "yes"),
            base_url=os.getenv("WITTY_INSIGHT_BASE_URL", "http://127.0.0.1:7396"),
            timeout_seconds=float(os.getenv("WITTY_INSIGHT_TIMEOUT_SECONDS", "10")),
            bearer_token=bearer_token,
        )


# ==============================================================================
# OpenCode 配置
# ==============================================================================


@dataclass(frozen=True)
class OpenCodeSettings:
    """OpenCode runtime 配置。

    环境变量:
        OPENCODE_STATE_DIR: opencode 状态存储目录, 默认 ~/.opencode
    """

    state_dir: str = "~/.opencode"

    @classmethod
    def from_env(cls) -> "OpenCodeSettings":
        return cls(
            state_dir=os.getenv("OPENCODE_STATE_DIR", "~/.opencode"),
        )

    def state_dir_resolved(self) -> Path:
        return Path(self.state_dir).expanduser().resolve(strict=False)


# ==============================================================================
# Runtime 统一配置
# ==============================================================================


@dataclass(frozen=True)
class RuntimeSettings:
    """Runtime 调度配置。

    每个 agent-server 实例运行 ``default_type`` 指定的单个 runtime
    ``supported`` 声明该镜像/部署能支持哪些 runtime 类型，供外界调度参考。

    环境变量:
        WITTY_RUNTIME_DEFAULT: 当前实例运行的 runtime 类型, 默认 openclaw
        WITTY_RUNTIME_SUPPORTED: 镜像支持的所有 runtime 类型, 默认 openclaw,opencode,dsh
    """

    default_type: str = "openclaw"
    supported: tuple[str, ...] = ("openclaw", "opencode", "dsh")

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
        default_type = os.getenv("WITTY_RUNTIME_DEFAULT", "openclaw")
        raw_supported = os.getenv("WITTY_RUNTIME_SUPPORTED", "openclaw,opencode,dsh")
        supported = tuple(
            item.strip() for item in raw_supported.split(",") if item.strip()
        )
        if not supported:
            supported = (default_type,)
        return cls(default_type=default_type, supported=supported)


# ==============================================================================
# 定时任务调度配置
# ==============================================================================


@dataclass(frozen=True)
class SchedulerSettings:
    """定时任务调度器配置。

    环境变量:
        WITTY_SCHEDULER_TZ: 默认时区 (IANA 名称), 默认 Asia/Shanghai
        WITTY_SCHEDULER_MISFIRE_GRACE_SECONDS: 错过触发后的宽限秒数, 默认 300
        WITTY_SCHEDULER_MAX_RUN_RECORDS: 每个任务保留的最大运行记录数, 默认 100
        WITTY_SCHEDULER_ORPHAN_RUN_TIMEOUT_SECONDS: 服务重启时视为孤儿运行的超时秒数, 默认 1800
    """

    timezone: str = "Asia/Shanghai"
    misfire_grace_seconds: int = 300
    max_run_records: int = 100
    orphan_run_timeout_seconds: int = 1800

    @classmethod
    def from_env(cls) -> "SchedulerSettings":
        timezone = os.getenv("WITTY_SCHEDULER_TZ", "Asia/Shanghai")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(
                f"Invalid timezone configured via WITTY_SCHEDULER_TZ: {timezone!r}"
            ) from exc
        return cls(
            timezone=timezone,
            misfire_grace_seconds=int(
                os.getenv("WITTY_SCHEDULER_MISFIRE_GRACE_SECONDS", "300")
            ),
            max_run_records=int(os.getenv("WITTY_SCHEDULER_MAX_RUN_RECORDS", "100")),
            orphan_run_timeout_seconds=int(
                os.getenv("WITTY_SCHEDULER_ORPHAN_RUN_TIMEOUT_SECONDS", "1800")
            ),
        )


# ==============================================================================
# 主配置类
# ==============================================================================


@dataclass(frozen=True)
class Settings:
    """Witty Service 统一配置容器

    整合所有子配置模块，通过 from_env() 类方法从环境变量加载配置。
    使用 get_settings() 函数获取单例配置实例。
    """

    auth_token: str
    cors: CorsSettings
    database: DatabaseSettings
    logging: LoggingSettings
    docker: DockerSettings
    openclaw_gateway: OpenClawGatewaySettings
    workspace: WorkspaceSettings
    openclaw: OpenClawSettings
    insight: InsightSettings
    opencode: OpenCodeSettings
    runtime: RuntimeSettings
    scheduler: SchedulerSettings

    @classmethod
    def from_env(cls) -> "Settings":
        """从环境变量加载所有配置"""
        return cls(
            auth_token=os.getenv("AUTH_TOKEN", "dev-token"),
            cors=CorsSettings(),
            database=DatabaseSettings.from_env(),
            logging=LoggingSettings.from_env(),
            docker=DockerSettings.from_env(),
            openclaw_gateway=OpenClawGatewaySettings.from_env(),
            workspace=WorkspaceSettings.from_env(),
            openclaw=OpenClawSettings.from_env(),
            insight=InsightSettings.from_env(),
            opencode=OpenCodeSettings.from_env(),
            runtime=RuntimeSettings.from_env(),
            scheduler=SchedulerSettings.from_env(),
        )


# ==============================================================================
# 配置访问接口
# ==============================================================================

_settings: Settings | None = None


def get_settings() -> Settings:
    """获取配置单例实例

    使用全局单例模式，首次调用时从环境变量加载配置，
    后续调用直接返回缓存的配置实例。

    Returns:
        Settings: 配置实例
    """
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings
