from __future__ import annotations

from sqlalchemy import inspect

from witty_service.persistence.db import create_sqlite_engine, init_db


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
        assert "agents" in tables
        assert "alembic_version" not in tables
    finally:
        engine.dispose()


def test_init_db_explicit_flag_overrides_environment(monkeypatch) -> None:
    monkeypatch.setenv("WITTY_DATABASE_AUTO_CREATE", "false")
    engine = create_sqlite_engine("sqlite:///:memory:")
    try:
        init_db(engine, auto_create=True)
        assert "agents" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
