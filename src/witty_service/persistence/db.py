from __future__ import annotations

import logging
from importlib.resources import files
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

_logger = logging.getLogger(__name__)


def create_sqlite_engine(database_url: str) -> Engine:
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False},
        future=True,
    )
    _configure_sqlite_engine(engine)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def init_db(engine: Engine, *, auto_create: bool | None = None) -> None:
    """在应用启动时自动执行数据库迁移（含新建表 & schema 变更）。"""
    if auto_create is None:
        from witty_service.config import get_settings

        auto_create = get_settings().database.auto_create
    if not auto_create:
        _logger.info("WITTY_DATABASE_AUTO_CREATE is false, skip auto migration")
        return

    _run_alembic_migrations(engine)


def _find_alembic_script_location() -> str:
    """查找 Alembic 迁移脚本所在的目录。

    优先级：
    1. 包内路径（pip install 后）：witty_service/alembic
    2. 开发环境：src/witty_service/alembic/ 目录
    """
    # 1. 尝试包内路径（pip install 后）
    try:
        pkg_alembic = files("witty_service") / "alembic"
        if pkg_alembic.is_dir():
            return str(pkg_alembic)
    except (ModuleNotFoundError, TypeError):
        pass

    # 2. 开发环境：src/witty_service/alembic/（与 alembic.ini 中 script_location 一致）
    dev_path = Path(__file__).resolve().parents[1] / "alembic"
    if dev_path.is_dir():
        return str(dev_path)

    raise FileNotFoundError(
        "Cannot find alembic script_location. "
        "Please ensure alembic migration scripts are installed with the package."
    )


def _run_alembic_migrations(engine: Engine) -> None:
    """使用 Alembic 执行数据库迁移到最新版本。"""
    from witty_service.config import get_settings

    settings = get_settings()

    alembic_cfg = AlembicConfig()
    alembic_cfg.set_main_option("script_location", _find_alembic_script_location())
    alembic_cfg.set_main_option("sqlalchemy.url", settings.database.url)

    _logger.info("Running Alembic migrations (upgrade head)...")
    try:
        with engine.connect() as connection:
            alembic_cfg.attributes["connection"] = connection
            _handle_legacy_db_if_needed(engine, alembic_cfg)
            alembic_command.upgrade(alembic_cfg, "head")
    except Exception:
        _logger.exception("Alembic migrations failed.")
        raise
    _logger.info("Alembic migrations completed.")


def _handle_legacy_db_if_needed(engine: Engine, alembic_cfg: AlembicConfig) -> None:
    """检测并处理存量数据库（由旧版 Base.metadata.create_all() 创建）。

    仅当迁移后的关键列/表均已存在时才 stamp head；否则 stamp 会把缺失迁移
    标记为已应用，运行期出现缺列/缺表错误。缺失对象记录 warning 并跳过 stamp。
    """
    from sqlalchemy import inspect

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    if "agents" not in existing_tables or "alembic_version" in existing_tables:
        return
    # 各迁移最终保留的关键对象（downgrade 才删除的列/表也属于最终 schema,必须校验）
    # 20260913_01 新增的 6 张渠道表必须在此登记：缺了它们，一个存量库会在迁移
    # 执行前被判为"结构完整"并 stamp head，渠道表永远不会被创建（实施计划 §4.1）。
    required_tables = {
        "mcp_servers",
        "channel_instances",
        "channel_provisionings",
        "channel_routes",
        "channel_inbound_events",
        "channel_deliveries",
        "channel_access_policies",
    }
    required_columns = {
        "sessions": {
            "runtime_type",
            "runtime_session_id",
            "runtime_session_key",
            # 20260913_01 新增的会话来源列
            "origin",
        },
        "models": {"compatibility"},
        "agents": {"model_id", "mcp_server_list"},
        "agent_skills": {
            "relative_path",
            "metadata",
            "skill_source",
            "skill_md_url",
        },
    }
    # 迁移新增的唯一约束(20260622_01),按名称 + 列集合校验
    required_unique: dict[str, dict[str, set[str]]] = {
        "sessions": {
            "uq_sessions_runtime_type_session_key": {
                "runtime_type",
                "runtime_session_key",
            },
            "uq_sessions_runtime_type_session_id": {
                "runtime_type",
                "runtime_session_id",
            },
        },
    }
    # 迁移替换的 check 约束(20260707_01),按 sqltext 全部关键值校验
    # (旧约束同名但内容旧——缺 clawhub/wittyhub 或 repo-id 规则旧——不得通过)
    required_checks: dict[str, dict[str, tuple[str, ...]]] = {
        "agent_skills": {
            "ck_agent_skills_source_type": ("wittyhub", "clawhub"),
            # repo-id 约束须同时含 wittyhub 与 clawhub 分支(缺 clawhub 的中间态不得通过)
            "ck_agent_skills_repo_id_by_source": ("wittyhub", "clawhub"),
        },
    }
    mcp_servers_columns = {
        "id",
        "mcp_server_name",
        "mcp_server_config",
        "created_at",
        "updated_at",
    }
    missing: list[str] = []
    for table in sorted(required_tables):
        if table not in existing_tables:
            missing.append(f"table {table}")
    for table, columns in required_columns.items():
        if table not in existing_tables:
            missing.append(f"table {table}")
            continue
        existing_cols = {c["name"] for c in inspector.get_columns(table)}
        for col in columns:
            if col not in existing_cols:
                missing.append(f"{table}.{col}")
    for table, named_columns in required_unique.items():
        if table not in existing_tables:
            continue
        existing_uniques = [
            (c.get("name"), set(c.get("column_names") or []))
            for c in inspector.get_unique_constraints(table)
            if c.get("name")
        ]
        for name, cols in named_columns.items():
            if not any(
                n == name and cols == cols_set for n, cols_set in existing_uniques
            ):
                missing.append(f"unique {table}.{name}")
    for table, checks in required_checks.items():
        if table not in existing_tables:
            continue
        existing_checks = {
            (c.get("name"), str(c.get("sqltext") or ""))
            for c in inspector.get_check_constraints(table)
        }
        for name, needles in checks.items():
            if not any(
                n == name and all(needle in text for needle in needles)
                for n, text in existing_checks
            ):
                missing.append(f"check {table}.{name}")
    if "mcp_servers" in existing_tables:
        existing_cols = {c["name"] for c in inspector.get_columns("mcp_servers")}
        for col in sorted(mcp_servers_columns - existing_cols):
            missing.append(f"mcp_servers.{col}")
    if missing:
        _logger.warning(
            "Legacy database is missing migration objects (%s); "
            "refusing to stamp head. Migrate the schema explicitly.",
            ", ".join(missing),
        )
        return
    _logger.info(
        "Detected legacy database (schema complete, no alembic_version). "
        "Stamping head to skip already-applied migrations."
    )
    alembic_command.stamp(alembic_cfg, "head")


def _configure_sqlite_engine(engine: Engine) -> None:
    """统一配置 SQLite 连接级 PRAGMA。

    ⚠️ 这里的 WAL / synchronous=NORMAL 不是"调优选项"，而是**可用性**前提。
    默认的 ``journal_mode=DELETE`` + ``synchronous=FULL`` 让每次 commit 都要在
    主库文件上做一次 fsync + 目录同步，本机（ext4/virtio）实测 **4.8 ms/次**。
    witty-service 的 WS 消费循环是"每收到一个事件就同步落库一次"，而 opencode
    这类 runtime 是**按 token** 下发 ``message.delta`` / ``thinking.delta``
    （实测峰值 151~176 事件/秒）——于是 150 × 4.8ms ≈ 720ms/s 的同步磁盘等待
    全部压在 asyncio 事件循环上。更糟的是 ``websockets`` 已经把帧收进内存队列时
    ``async for`` 不会真正让出循环，落库会在**不回到事件循环**的情况下连续跑完
    整个积压（实测单次 30 s），期间 uvicorn 的 ws keepalive ping 拿不到 pong
    （默认 ping_interval=20s / ping_timeout=20s），连接被以
    ``1011 keepalive ping timeout`` 掐断 —— 前端看到的就是"任务突然变 error"。

    WAL 把 commit 变成对 ``-wal`` 的顺序追加（实测 0.032 ms/次，约 150 倍），
    ``synchronous=NORMAL`` 只在 checkpoint 时 fsync：WAL 下应用崩溃不丢已提交
    事务，只有整机掉电才可能丢最后几条 —— 对本服务（可由 runtime 重放/重试）
    是合适的取舍。
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        # journal_mode 会返回当前模式（内存库返回 "memory"），必须取走结果集。
        cursor.execute("PRAGMA journal_mode=WAL").fetchall()
        cursor.execute("PRAGMA synchronous=NORMAL")
        # 多会话/多线程并发写时不要立刻抛 "database is locked"。
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


#: WAL 定期 checkpoint 的等待上限。TRUNCATE 模式在有活跃读事务时会通过
#: busy-handler 等待读者；超限后返回 busy（不报错），留给下一次周期重试。
CHECKPOINT_BUSY_TIMEOUT_MS = 5_000


def checkpoint_database() -> bool:
    """对主库执行一次 ``wal_checkpoint(TRUNCATE)``，成功时把 ``-wal`` 截断为 0。

    - 独立短连接（从 settings 读 URL），不依赖 engine/连接池的注入路径；
    - busy 超时 5s：碰上活跃读事务就放弃本次（返回 False），绝不长时间阻塞；
    - 只能低频调用（定时器 / delete_agent 后）。若放进事件消费循环高频执行，
      等于退化回"每次 commit 都 fsync"，会重新触发上面注释里的事件循环压死事故。
    """
    import sqlite3

    from witty_service.config import get_settings

    database_url = get_settings().database.url
    if not database_url.startswith("sqlite:///"):
        _logger.debug("checkpoint skipped: non-sqlite database")
        return False
    db_path = database_url.replace("sqlite:///", "")
    try:
        # connect(timeout=...) 即 busy_timeout，限制 TRUNCATE 等待读者的时长。
        conn = sqlite3.connect(db_path, timeout=CHECKPOINT_BUSY_TIMEOUT_MS / 1000)
        try:
            result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        _logger.warning("wal_checkpoint failed", exc_info=True)
        return False
    # 返回 (busy, log_pages, checkpointed_pages)；busy=1 表示有读者未放行，未完成。
    busy = bool(result and result[0])
    if busy:
        _logger.info("wal_checkpoint deferred (busy): retried on next schedule")
        return False
    _logger.info("wal_checkpoint(TRUNCATE) completed: %s", result)
    return True
