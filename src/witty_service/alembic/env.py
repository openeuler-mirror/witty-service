from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool

from witty_service.config import get_settings
from witty_service.persistence.entities import Base

config = context.config

# 仅在通过 alembic CLI 调用（有配置文件）时才使用 fileConfig
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# 仅当 sqlalchemy.url 未通过程序化方式设置时，从配置中获取
if not config.get_main_option("sqlalchemy.url"):
    database_url = get_settings().database.url
    if database_url:
        config.set_main_option("sqlalchemy.url", database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"foreign key violations after migration: {violations}")


def run_migrations_online() -> None:
    """在线模式执行迁移。

    优先使用通过 cfg.attributes['connection'] 传入的连接（程序化调用），
    否则从 alembic 配置文件自行创建引擎（CLI 调用）。
    """
    connectable = config.attributes.get("connection")
    if connectable is not None:
        _run_migrations(connectable)
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    @event.listens_for(connectable, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    with connectable.connect() as connection:
        _run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
