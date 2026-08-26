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
    required_tables = {"mcp_servers"}
    required_columns = {
        "sessions": {"runtime_type", "runtime_session_id", "runtime_session_key"},
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
            if not any(n == name and cols == cols_set for n, cols_set in existing_uniques):
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
                n == name and all(nd in text for nd in needles)
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

def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
