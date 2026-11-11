from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from witty_service.persistence.consistency import StructureCheckError, verify_structure
from witty_service.persistence.db import create_sqlite_engine
from witty_service.persistence.entities import Base
from witty_service.persistence.repository import CatalogRepository
from witty_service.persistence.seed import seed_defaults


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'db.sqlite3'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _seeded(engine: Engine, tmp_path: Path) -> tuple[sessionmaker[Session], str, str]:
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    catalog = CatalogRepository(factory)
    model = catalog.create_model(
        "Model", "provider", "model-1", None, None, None, 8192, 0.7, 1_000
    )
    catalog.set_default_model(model.id, 1_001)
    result = seed_defaults(factory, host_directory=tmp_path / "workspace")
    return factory, result.primary_agent.id, model.id


def _issues(engine: Engine) -> list[str]:
    with pytest.raises(StructureCheckError) as exc_info:
        verify_structure(engine)
    return exc_info.value.issues


def test_verify_structure_accepts_seeded_database(
    engine: Engine, tmp_path: Path
) -> None:
    _seeded(engine, tmp_path)

    verify_structure(engine)


def test_verify_structure_reports_missing_tables(tmp_path: Path) -> None:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'empty.sqlite3'}")
    try:
        issues = _issues(engine)
    finally:
        engine.dispose()

    assert issues[0].startswith("missing tables: ")


def test_verify_structure_requires_one_primary_and_one_default_workspace(
    engine: Engine, tmp_path: Path
) -> None:
    _seeded(engine, tmp_path)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "update agent_definitions set archived_at = 1 where kind = 'primary'"
        )
        connection.exec_driver_sql("update workspaces set is_default = 0")

    issues = _issues(engine)

    assert "expected exactly one active primary agent, found 0" in issues
    assert "expected exactly one default workspace, found 0" in issues


def test_verify_structure_requires_foreign_keys_pragma(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'plain.sqlite3'}")
    Base.metadata.create_all(engine)
    try:
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        seed_defaults(factory, host_directory=tmp_path / "workspace")
        issues = _issues(engine)
    finally:
        engine.dispose()

    assert "PRAGMA foreign_keys is not enabled" in issues


def test_verify_structure_reports_referential_integrity_errors(
    engine: Engine, tmp_path: Path
) -> None:
    _, _, model_id = _seeded(engine, tmp_path)
    connection = sqlite3.connect(tmp_path / "db.sqlite3")
    try:
        connection.execute("pragma foreign_keys = off")
        connection.execute("delete from models where id = ?", (model_id,))
        connection.commit()
    finally:
        connection.close()

    issues = _issues(engine)

    assert any(issue.startswith("foreign_key_check failed: ") for issue in issues)


def test_verify_structure_reports_orphan_turns(engine: Engine, tmp_path: Path) -> None:
    _, agent_id, model_id = _seeded(engine, tmp_path)
    database = tmp_path / "db.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("pragma foreign_keys = off")
        connection.execute(
            "insert into turns (id, session_id, turn_seq, agent_id, model_id, "
            "agent_name_snapshot, model_name_snapshot, status, started_at, "
            "finished_at, created_at) values "
            "('orphan', 'missing-session', 1, ?, ?, 'Agent', 'Model', "
            "'completed', 1, 1, 1)",
            (agent_id, model_id),
        )
        connection.commit()
    finally:
        connection.close()

    issues = _issues(engine)

    assert "found 1 orphan turns" in issues
