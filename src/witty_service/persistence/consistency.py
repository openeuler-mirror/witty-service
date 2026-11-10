from __future__ import annotations

from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import OperationalError

from witty_service.persistence.entities import Base


class StructureCheckError(RuntimeError):
    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues))


def verify_structure(engine: Engine) -> None:
    with engine.connect() as connection:
        issues = _collect_issues(connection)
    if issues:
        raise StructureCheckError(issues)


def _collect_issues(connection: Connection) -> list[str]:
    issues: list[str] = []
    version = tuple(
        int(part)
        for part in connection.exec_driver_sql("select sqlite_version()")
        .scalar_one()
        .split(".")
    )
    if version < (3, 35, 0):
        issues.append(f"sqlite version {'.'.join(map(str, version))} is below 3.35.0")
    try:
        json1_available = connection.exec_driver_sql(
            "select json_valid('{}')"
        ).scalar_one()
    except OperationalError:
        json1_available = 0
    if json1_available != 1:
        issues.append("SQLite JSON1 extension is unavailable")
    if connection.exec_driver_sql("pragma foreign_keys").scalar_one() != 1:
        issues.append("PRAGMA foreign_keys is not enabled")
    integrity = [row[0] for row in connection.exec_driver_sql("pragma integrity_check")]
    if integrity != ["ok"]:
        issues.append(f"integrity_check failed: {integrity}")
    foreign_key_errors = connection.exec_driver_sql(
        "pragma foreign_key_check"
    ).fetchall()
    if foreign_key_errors:
        issues.append(f"foreign_key_check failed: {foreign_key_errors}")

    tables = set(inspect(connection).get_table_names())
    missing = set(Base.metadata.tables) - tables
    if missing:
        issues.append(f"missing tables: {sorted(missing)}")
        return issues

    primary_count = connection.execute(
        text(
            "select count(*) from agent_definitions "
            "where kind = 'primary' and archived_at is null"
        )
    ).scalar_one()
    if primary_count != 1:
        issues.append(
            f"expected exactly one active primary agent, found {primary_count}"
        )

    default_count = connection.execute(
        text("select count(*) from workspaces where is_default = 1")
    ).scalar_one()
    if default_count != 1:
        issues.append(f"expected exactly one default workspace, found {default_count}")

    orphan_turns = connection.execute(
        text(
            "select count(*) from turns t "
            "left join sessions s on s.id = t.session_id "
            "where s.id is null"
        )
    ).scalar_one()
    if orphan_turns:
        issues.append(f"found {orphan_turns} orphan turns")

    return issues
