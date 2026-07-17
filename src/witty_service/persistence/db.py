from __future__ import annotations

import os

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from witty_service.persistence.orm import Base


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
    """Bootstrap schema only for tests or explicitly opted-in local setups."""
    if auto_create is None:
        auto_create = os.getenv("WITTY_DATABASE_AUTO_CREATE", "false").lower() in (
            "1",
            "true",
            "yes",
        )
    if not auto_create:
        return

    _migrate_models_table(engine)
    Base.metadata.create_all(bind=engine)


def _migrate_models_table(engine: Engine) -> None:
    """自动迁移 models 表，添加缺失的 compatibility 列"""
    inspector = inspect(engine)
    if inspector.has_table("models"):
        columns = inspector.get_columns("models")
        column_names = {col["name"] for col in columns}
        if "compatibility" not in column_names:
            with engine.connect() as conn:
                conn.execute(text("ALTER TABLE models ADD COLUMN compatibility VARCHAR(16)"))
                conn.commit()


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
