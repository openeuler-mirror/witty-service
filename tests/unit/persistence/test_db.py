from __future__ import annotations

from sqlalchemy import (
    CheckConstraint,
    Column,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
)

from witty_service.persistence.db import (
    _handle_legacy_db_if_needed,
    create_sqlite_engine,
    init_db,
)


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
        assert "messages" in tables
        # alembic_version 是 alembic 自动创建用于追踪迁移版本的表
        assert "alembic_version" in tables
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


def _stamp_spy(monkeypatch, calls: list) -> None:
    """把 alembic_command.stamp 换成记录器,避免真实迁移。"""
    import witty_service.persistence.db as db_mod

    def fake_stamp(_cfg, _revision) -> None:
        calls.append(_revision)

    monkeypatch.setattr(db_mod.alembic_command, "stamp", fake_stamp)


def _complete_legacy_schema(
    meta: MetaData,
    *,
    old_source_check: bool = False,
    old_repo_check: bool = False,
    mid_repo_check: bool = False,
) -> None:
    """构造迁移后 schema 完整的 legacy 库(全部最终保留对象,含约束)。

    old_source_check=True:source-type check 为旧版(仅 builtin/git/local)。
    old_repo_check=True:repo-id check 为旧版(仅 git/local,不含 clawhub/wittyhub)。
    mid_repo_check=True:repo-id check 含 wittyhub 但缺 clawhub 分支(中间态)。
    """
    Table(
        "agents",
        meta,
        Column("id", String(36), primary_key=True),
        Column("model_id", String(36)),
        Column("mcp_server_list", String(255)),
    )
    Table(
        "sessions",
        meta,
        Column("id", String(36), primary_key=True),
        Column("runtime_type", String(32)),
        Column("runtime_session_id", String(255)),
        Column("runtime_session_key", Text()),
        UniqueConstraint(
            "runtime_type", "runtime_session_key",
            name="uq_sessions_runtime_type_session_key",
        ),
        UniqueConstraint(
            "runtime_type", "runtime_session_id",
            name="uq_sessions_runtime_type_session_id",
        ),
    )
    Table(
        "models",
        meta,
        Column("id", String(36), primary_key=True),
        Column("compatibility", String(16)),
    )
    Table(
        "agent_skills",
        meta,
        Column("id", String(36), primary_key=True),
        Column("source_type", String(32)),
        Column("repo_id", String(36)),
        Column("relative_path", String(255)),
        Column("metadata", String(255)),
        Column("skill_source", String(255)),
        Column("skill_md_url", String(255)),
        CheckConstraint(
            "source_type IN ('builtin', 'git', 'local', 'clawhub', 'wittyhub')"
            if not old_source_check
            else "source_type IN ('builtin', 'git', 'local')",
            name="ck_agent_skills_source_type",
        ),
        CheckConstraint(
            "(source_type IN ('git', 'local', 'clawhub') AND repo_id IS NOT NULL) OR "
            "(source_type IN ('builtin', 'wittyhub') AND repo_id IS NULL)"
            if not old_repo_check and not mid_repo_check
            else (
                "(source_type IN ('git', 'local') AND repo_id IS NOT NULL) OR "
                "(source_type IN ('builtin', 'wittyhub') AND repo_id IS NULL)"
                if mid_repo_check
                else "(source_type IN ('git', 'local') AND repo_id IS NOT NULL)"
            ),
            name="ck_agent_skills_repo_id_by_source",
        ),
    )
    Table(
        "mcp_servers",
        meta,
        Column("id", String(36), primary_key=True),
        Column("mcp_server_name", String(255)),
        Column("mcp_server_config", String(255)),
        Column("created_at", String(64)),
        Column("updated_at", String(64)),
    )


def test_legacy_db_schema_complete_stamps_head(monkeypatch) -> None:
    """schema 完整(含迁移后全部关键列/表)的 legacy 库才 stamp head。"""
    engine = create_engine("sqlite:///:memory:")
    meta = MetaData()
    _complete_legacy_schema(meta)
    meta.create_all(engine)
    calls: list = []
    _stamp_spy(monkeypatch, calls)
    _handle_legacy_db_if_needed(engine, None)
    assert calls == ["head"]


def test_legacy_db_missing_migration_objects_skips_stamp(monkeypatch) -> None:
    """缺迁移后关键对象的 legacy 库不得 stamp(否则运行期缺列/缺表)。"""
    engine = create_engine("sqlite:///:memory:")
    meta = MetaData()
    Table("agents", meta, Column("id", String(36), primary_key=True))  # 无 model_id/mcp_server_list
    Table("sessions", meta, Column("id", String(36), primary_key=True))  # 无 runtime_*
    meta.create_all(engine)
    calls: list = []
    _stamp_spy(monkeypatch, calls)
    _handle_legacy_db_if_needed(engine, None)
    assert calls == []


def test_legacy_db_missing_compatibility_or_mcp_servers_skips_stamp(monkeypatch) -> None:
    """仅缺 models.compatibility 或 mcp_servers 表(Codex 复现场景)也不得 stamp。"""
    engine = create_engine("sqlite:///:memory:")
    meta = MetaData()
    _complete_legacy_schema(meta)
    # 从 meta 移除定义(模拟旧库缺这些对象),再建其余表
    meta.remove(meta.tables["models"])
    meta.remove(meta.tables["mcp_servers"])
    meta.create_all(engine)
    calls: list = []
    _stamp_spy(monkeypatch, calls)
    _handle_legacy_db_if_needed(engine, None)
    assert calls == []


def test_legacy_db_old_source_check_skips_stamp(monkeypatch) -> None:
    """source-type check 仍为旧版(不含 wittyhub/clawhub)时不得 stamp。"""
    engine = create_engine("sqlite:///:memory:")
    meta = MetaData()
    _complete_legacy_schema(meta, old_source_check=True)
    meta.create_all(engine)
    calls: list = []
    _stamp_spy(monkeypatch, calls)
    _handle_legacy_db_if_needed(engine, None)
    assert calls == []


def test_legacy_db_old_repo_check_skips_stamp(monkeypatch) -> None:
    """source-type 已更新但 repo-id check 仍旧(Codex 第九轮复现)时不得 stamp。"""
    engine = create_engine("sqlite:///:memory:")
    meta = MetaData()
    _complete_legacy_schema(meta, old_repo_check=True)
    meta.create_all(engine)
    calls: list = []
    _stamp_spy(monkeypatch, calls)
    _handle_legacy_db_if_needed(engine, None)
    assert calls == []


def test_legacy_db_mid_repo_check_skips_stamp(monkeypatch) -> None:
    """repo-id check 含 wittyhub 但缺 clawhub 分支(Codex 第十轮复现)时不得 stamp。"""
    engine = create_engine("sqlite:///:memory:")
    meta = MetaData()
    _complete_legacy_schema(meta, mid_repo_check=True)
    meta.create_all(engine)
    calls: list = []
    _stamp_spy(monkeypatch, calls)
    _handle_legacy_db_if_needed(engine, None)
    assert calls == []
