from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from witty_service.persistence.db import create_sqlite_engine, init_db
from witty_service.persistence.entities import Base


def test_init_db_skips_schema_bootstrap_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv("WITTY_DATABASE_AUTO_CREATE", "false")
    engine = create_sqlite_engine("sqlite:///:memory:")
    try:
        init_db(engine)
        assert inspect(engine).get_table_names() == []
    finally:
        engine.dispose()


def test_init_db_bootstraps_schema_when_enabled(monkeypatch) -> None:
    monkeypatch.setenv("WITTY_DATABASE_AUTO_CREATE", "true")
    engine = create_sqlite_engine("sqlite:///:memory:")
    try:
        init_db(engine)
        tables = set(inspect(engine).get_table_names())
        assert tables == set(Base.metadata.tables) | {"alembic_version"}
    finally:
        engine.dispose()


def test_init_db_explicit_flag_overrides_environment(monkeypatch) -> None:
    monkeypatch.setenv("WITTY_DATABASE_AUTO_CREATE", "false")
    engine = create_engine("sqlite:///:memory:")
    try:
        init_db(engine, auto_create=True)
        assert "workspaces" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_init_db_rejects_legacy_schema_without_writing(tmp_path) -> None:
    db_path = tmp_path / "legacy.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE agents (id VARCHAR(36) PRIMARY KEY)"))
    try:
        with pytest.raises(RuntimeError, match="not compatible"):
            init_db(engine, auto_create=True)
        assert set(inspect(engine).get_table_names()) == {"agents"}
    finally:
        engine.dispose()
