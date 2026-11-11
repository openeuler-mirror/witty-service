from __future__ import annotations

import logging
from importlib.resources import files
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event, inspect
from sqlalchemy.orm import Session, sessionmaker

from witty_service.persistence.entities import Base

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

    existing_tables = set(inspect(engine).get_table_names())
    unexpected_tables = existing_tables - set(Base.metadata.tables) - {"alembic_version"}
    if unexpected_tables or (existing_tables and "alembic_version" not in existing_tables):
        raise RuntimeError(
            "database is not compatible with the agent session workspace baseline "
            f"(existing tables: {', '.join(sorted(existing_tables)) or 'none'}). "
            "Move the existing database file aside and start with a fresh one."
        )

    _logger.info("Running Alembic migrations (upgrade head)...")
    try:
        with engine.connect() as connection:
            alembic_cfg.attributes["connection"] = connection
            alembic_command.upgrade(alembic_cfg, "head")
    except Exception:
        _logger.exception("Alembic migrations failed.")
        raise
    _logger.info("Alembic migrations completed.")


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
