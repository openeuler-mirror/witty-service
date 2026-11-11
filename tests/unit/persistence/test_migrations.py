from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from witty_service.persistence.entities import Base

ROOT = Path(__file__).resolve().parents[3]


def _alembic_config(db_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "src/witty_service/alembic"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


@pytest.fixture()
def migrated_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "migrated.sqlite3"
    command.upgrade(_alembic_config(db_path), "head")
    return db_path


@pytest.fixture()
def metadata_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "metadata.sqlite3"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return db_path


def _normalized(value: str | None) -> str | None:
    return None if value is None else re.sub(r"\s+", " ", value).strip()


def _schema_snapshot(db_path: Path) -> dict[str, object]:
    connection = sqlite3.connect(db_path)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
                "AND name != 'alembic_version' ORDER BY name"
            )
        ]
        snapshot: dict[str, object] = {}
        for table in tables:
            columns = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            foreign_keys = sorted(
                row[1:]
                for row in connection.execute(
                    f'PRAGMA foreign_key_list("{table}")'
                ).fetchall()
            )
            snapshot[table] = (columns, foreign_keys)
        return snapshot
    finally:
        connection.close()


def _index_sql(db_path: Path) -> dict[str, str]:
    connection = sqlite3.connect(db_path)
    try:
        return {
            name: _normalized(sql) or ""
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND sql IS NOT NULL"
            )
        }
    finally:
        connection.close()


def _table_sql(db_path: Path, table: str) -> str:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return _normalized(row[0]) if row and row[0] else ""
    finally:
        connection.close()


def _check_constraints(db_path: Path) -> dict[str, dict[str, str]]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        return {
            table: {
                constraint["name"]: _normalized(constraint["sqltext"]) or ""
                for constraint in inspector.get_check_constraints(table)
            }
            for table in inspector.get_table_names()
            if table != "alembic_version"
        }
    finally:
        engine.dispose()


def test_single_baseline_revision() -> None:
    script = ScriptDirectory.from_config(_alembic_config(Path("/tmp/unused.sqlite3")))
    revisions = list(script.walk_revisions())
    assert len(revisions) == 1
    assert revisions[0].down_revision is None
    assert script.get_heads() == [revisions[0].revision]


def test_upgrade_is_repeatable(migrated_db: Path) -> None:
    command.upgrade(_alembic_config(migrated_db), "head")
    command.upgrade(_alembic_config(migrated_db), "head")
    assert set(
        inspect(create_engine(f"sqlite:///{migrated_db}")).get_table_names()
    ) == (set(Base.metadata.tables) | {"alembic_version"})


def test_downgrade_to_base_succeeds(migrated_db: Path) -> None:
    command.downgrade(_alembic_config(migrated_db), "base")
    engine = create_engine(f"sqlite:///{migrated_db}")
    try:
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    finally:
        engine.dispose()


def test_schema_matches_orm_metadata(migrated_db: Path, metadata_db: Path) -> None:
    assert _schema_snapshot(migrated_db) == _schema_snapshot(metadata_db)


def test_check_constraints_match_orm_metadata(
    migrated_db: Path, metadata_db: Path
) -> None:
    assert _check_constraints(migrated_db) == _check_constraints(metadata_db)


def test_indexes_match_orm_metadata(migrated_db: Path, metadata_db: Path) -> None:
    assert _index_sql(migrated_db) == _index_sql(metadata_db)


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("agent_definitions", "name"),
        ("skill_repo", "repo_name"),
        ("skills", "skill_name"),
        ("mcp_servers", "mcp_server_name"),
    ],
)
def test_nocase_columns_keep_collation(
    migrated_db: Path, metadata_db: Path, table: str, column: str
) -> None:
    pattern = re.compile(rf"\b{column}\b[^,]*COLLATE \"NOCASE\"")
    assert pattern.search(_table_sql(migrated_db, table))
    assert pattern.search(_table_sql(metadata_db, table))


def test_models_schema_includes_secret_revision_and_provider_unique(
    migrated_db: Path,
) -> None:
    engine = create_engine(f"sqlite:///{migrated_db}")
    try:
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("models")}
        checks = {
            constraint["name"]
            for constraint in inspector.get_check_constraints("models")
        }
        uniques = {
            constraint["name"]
            for constraint in inspector.get_unique_constraints("models")
        }
    finally:
        engine.dispose()

    assert "secret_revision" in columns
    assert "ck_models_secret_revision" in checks
    assert "uq_models_provider_model" in uniques


def test_channel_waiting_expression_index_is_physical(migrated_db: Path) -> None:
    indexes = _index_sql(migrated_db)
    assert indexes["uq_channel_provisionings_waiting"] == (
        "CREATE UNIQUE INDEX uq_channel_provisionings_waiting ON channel_provisionings "
        "(channel, coalesce(owner_ref, '')) WHERE status = 'waiting'"
    )
