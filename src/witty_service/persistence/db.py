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
    _enable_sqlite_foreign_keys(engine)
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
    """检测并处理存量数据库（由旧版 Base.metadata.create_all() 创建）。"""
    from sqlalchemy import inspect

    existing_tables = inspect(engine).get_table_names()
    if "agents" in existing_tables and "alembic_version" not in existing_tables:
        _logger.info(
            "Detected legacy database (tables exist but no alembic_version). "
            "Stamping head to skip already-applied migrations."
        )
        alembic_command.stamp(alembic_cfg, "head")

def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
