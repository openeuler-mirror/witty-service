"""src/witty_service/persistence/orm.py 的单元测试。

策略:
- 用 SQLite in-memory 数据库 + db.py 的工具函数
- Base.metadata.create_all 建表,逐个 ORM 测试:
  * 默认值
  * 必填字段缺失会报错
  * 增删改查
  * 唯一约束 / CheckConstraint
  * 外键级联 (CASCADE / SET NULL)
  * 枚举字段、JSON 字段、日期时间字段
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import UniqueConstraint, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from witty_service.persistence.db import (
    create_session_factory,
    create_sqlite_engine,
    init_db,
)
from witty_service.persistence.orm import (
    AgentLockORM,
    AgentORM,
    AgentRuntimeStateORM,
    AgentSkillORM,
    Base,
    McpServerORM,
    MessageEventORM,
    MessageORM,
    MessageStatus,
    ModelORM,
    SessionORM,
    SessionStatus,
    SkillORM,
    SkillRepositoryORM,
    utcnow,
)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture()
def session() -> Session:
    """每个测试一个全新的 in-memory SQLite session。"""
    engine = create_sqlite_engine("sqlite:///:memory:")
    init_db(engine, auto_create=True)
    factory = create_session_factory(engine)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _new_agent(
    agent_id: str = "agent-1",
    *,
    name: str = "demo",
    status: str = "running",
    **overrides,
) -> AgentORM:
    defaults: dict = dict(
        id=agent_id,
        name=name,
        description="",
        sandbox_type="local_process",
        adapter_type="http",
        status=status,
        workspace_path="/tmp/workspace",
        idle_timeout_seconds=300,
        mcp_server_list=[],
    )
    defaults.update(overrides)
    return AgentORM(**defaults)


# ---------------------------------------------------------------------------
# 工具函数 / 枚举
# ---------------------------------------------------------------------------


def test_utcnow_returns_aware_utc_datetime() -> None:
    result = utcnow()
    assert isinstance(result, datetime)
    assert result.tzinfo is not None
    assert result.utcoffset().total_seconds() == 0


def test_session_status_enum_values() -> None:
    assert SessionStatus.running.value == "running"
    assert SessionStatus.idle.value == "idle"
    assert SessionStatus.error.value == "error"


def test_message_status_enum_values() -> None:
    assert MessageStatus.generating.value == "generating"
    assert MessageStatus.completed.value == "completed"
    assert MessageStatus.error.value == "error"
    assert MessageStatus.interrupted.value == "interrupted"


def test_base_metadata_contains_all_tables() -> None:
    expected = {
        "agents",
        "agent_runtime_state",
        "sessions",
        "messages",
        "message_events",
        "agent_locks",
        "models",
        "skill_repo",
        "skills",
        "agent_skills",
        "mcp_servers",
    }
    assert set(Base.metadata.tables.keys()) >= expected


def test_session_table_schema_includes_runtime_identity_columns() -> None:
    table = Base.metadata.tables["sessions"]

    assert {"runtime_type", "runtime_session_id", "runtime_session_key"} <= set(
        table.columns.keys()
    )

    unique_names = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_sessions_runtime_type_session_id" in unique_names
    assert "uq_sessions_runtime_type_session_key" in unique_names


# ---------------------------------------------------------------------------
# AgentORM
# ---------------------------------------------------------------------------


def test_agent_orm_default_values() -> None:
    agent = _new_agent()
    assert agent.description == ""
    assert agent.mcp_server_list == []
    assert agent.sandbox_id is None
    assert agent.model_id is None
    assert agent.last_active_at is None


def test_agent_orm_create_and_query(session: Session) -> None:
    now = utcnow()
    agent = _new_agent(
        last_active_at=now, model_id="model-1", mcp_server_list=["x", "y"]
    )
    session.add(agent)
    session.commit()

    fetched = session.get(AgentORM, "agent-1")
    assert fetched is not None
    assert fetched.name == "demo"
    assert fetched.model_id == "model-1"
    assert fetched.mcp_server_list == ["x", "y"]
    assert fetched.last_active_at == now
    assert fetched.created_at is not None
    assert fetched.updated_at is not None
    # timezone-aware UTC
    assert fetched.created_at.tzinfo is not None


def test_agent_orm_updated_at_bumps_on_update(session: Session) -> None:
    import time

    agent = _new_agent()
    session.add(agent)
    session.commit()
    session.expire_all()
    original_updated = session.get(AgentORM, agent.id).updated_at

    time.sleep(0.05)
    agent.name = "renamed"
    session.commit()
    session.expire_all()
    refreshed = session.get(AgentORM, agent.id)
    assert refreshed.updated_at > original_updated


def test_agent_orm_missing_required_field_raises(session: Session) -> None:
    session.add(AgentORM(id="x"))  # 缺 name / sandbox_type 等
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# AgentRuntimeStateORM
# ---------------------------------------------------------------------------


def test_agent_runtime_state_defaults(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    state = AgentRuntimeStateORM(agent_id="agent-1")
    session.add(state)
    session.commit()

    fetched = session.get(AgentRuntimeStateORM, "agent-1")
    assert fetched is not None
    assert fetched.runtime_payload_json == {}
    assert fetched.adapter_base_url is None
    assert fetched.adapter_ready is False
    assert fetched.last_error is None


def test_agent_runtime_state_cascade_delete(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    state = AgentRuntimeStateORM(
        agent_id="agent-1", adapter_base_url="http://x", adapter_ready=True
    )
    state.runtime_payload_json = {"foo": "bar"}
    session.add(state)
    session.commit()

    session.delete(session.get(AgentORM, "agent-1"))
    session.commit()
    session.expire_all()
    assert session.get(AgentRuntimeStateORM, "agent-1") is None


# ---------------------------------------------------------------------------
# SessionORM
# ---------------------------------------------------------------------------


def test_session_orm_defaults(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    s = SessionORM(id="sess-1", agent_id="agent-1")
    session.add(s)
    session.commit()

    fetched = session.get(SessionORM, "sess-1")
    assert fetched is not None
    assert fetched.status == SessionStatus.idle
    assert fetched.title is None
    assert fetched.pinned is False
    assert fetched.remote_runtime_agent_id is None
    assert fetched.runtime_type is None
    assert fetched.runtime_session_id is None
    assert fetched.runtime_session_key is None


def test_session_orm_runtime_identity_uniqueness(session: Session) -> None:
    session.add_all([_new_agent("agent-1"), _new_agent("agent-2")])
    session.commit()
    session.add(
        SessionORM(
            id="sess-1",
            agent_id="agent-1",
            runtime_type="openclaw",
            runtime_session_id="runtime-1",
            runtime_session_key="agent:agent-1:session:sess-1",
        )
    )
    session.commit()

    session.add(
        SessionORM(
            id="sess-2",
            agent_id="agent-2",
            runtime_type="openclaw",
            runtime_session_id="runtime-1",
            runtime_session_key="agent:agent-2:session:sess-2",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()

    session.add(
        SessionORM(
            id="sess-3",
            agent_id="agent-2",
            runtime_type="openclaw",
            runtime_session_id="runtime-3",
            runtime_session_key="agent:agent-1:session:sess-1",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_session_orm_enum_persists_as_string(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    s = SessionORM(id="sess-1", agent_id="agent-1", status=SessionStatus.running)
    session.add(s)
    session.commit()

    # 读回时仍是 enum
    fetched = session.get(SessionORM, "sess-1")
    assert fetched.status == SessionStatus.running

    # 原始数据库里存的是字符串
    raw = session.execute(
        select(SessionORM.__table__.c.status).where(SessionORM.id == "sess-1")
    ).scalar_one()
    assert raw == "running"


def test_session_orm_cascade_delete_with_agent(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    session.add(SessionORM(id="sess-1", agent_id="agent-1"))
    session.commit()
    session.delete(session.get(AgentORM, "agent-1"))
    session.commit()
    assert session.get(SessionORM, "sess-1") is None


# ---------------------------------------------------------------------------
# MessageORM
# ---------------------------------------------------------------------------


def _new_session(session: Session, sid: str = "sess-1") -> None:
    if session.get(AgentORM, "agent-1") is None:
        session.add(_new_agent())
    session.add(SessionORM(id=sid, agent_id="agent-1"))
    session.commit()


def test_message_orm_defaults(session: Session) -> None:
    _new_session(session)
    m = MessageORM(
        id="msg-1",
        agent_id="agent-1",
        session_id="sess-1",
        role="user",
        content="hello",
    )
    session.add(m)
    session.commit()

    fetched = session.get(MessageORM, "msg-1")
    assert fetched is not None
    assert fetched.metadata_json == {}
    assert fetched.status == MessageStatus.completed
    assert fetched.last_stream_at is None


def test_message_orm_json_round_trip(session: Session) -> None:
    _new_session(session)
    payload = {"tokens": 3, "tags": ["a", "b"], "nested": {"k": "v"}}
    m = MessageORM(
        id="msg-1",
        agent_id="agent-1",
        session_id="sess-1",
        role="assistant",
        content="hi",
        metadata_json=payload,
        status=MessageStatus.generating,
    )
    session.add(m)
    session.commit()
    session.expire_all()

    fetched = session.get(MessageORM, "msg-1")
    assert fetched.metadata_json == payload
    assert fetched.status == MessageStatus.generating


def test_message_orm_indexes_present() -> None:
    indexes = MessageORM.__table__.indexes
    index_names = {idx.name for idx in indexes}
    assert "ix_messages_session_created" in index_names
    assert "ix_messages_session_status" in index_names


def test_message_orm_cascade_delete_with_session(session: Session) -> None:
    _new_session(session)
    m = MessageORM(
        id="msg-1",
        agent_id="agent-1",
        session_id="sess-1",
        role="user",
        content="x",
    )
    session.add(m)
    session.commit()
    session.delete(session.get(SessionORM, "sess-1"))
    session.commit()
    session.expire_all()
    assert session.get(MessageORM, "msg-1") is None


# ---------------------------------------------------------------------------
# MessageEventORM
# ---------------------------------------------------------------------------


def test_message_event_orm_unique_seq_per_session(session: Session) -> None:
    _new_session(session)
    session.add(
        MessageORM(
            id="msg-1",
            agent_id="agent-1",
            session_id="sess-1",
            role="user",
            content="x",
        )
    )
    session.commit()

    session.add(
        MessageEventORM(
            id="evt-1",
            agent_id="agent-1",
            session_id="sess-1",
            message_id="msg-1",
            event_type="delta",
            payload_json={"text": "hi"},
            seq_no=1,
        )
    )
    session.commit()

    # 同一 (session_id, seq_no) 再插会冲突
    session.add(
        MessageEventORM(
            id="evt-2",
            agent_id="agent-1",
            session_id="sess-1",
            message_id="msg-1",
            event_type="delta",
            payload_json={"text": "again"},
            seq_no=1,
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_message_event_orm_seq_index_present() -> None:
    indexes = MessageEventORM.__table__.indexes
    assert any(idx.name == "ix_message_events_msg_seq" for idx in indexes)


def test_message_event_orm_set_null_on_message_delete(session: Session) -> None:
    _new_session(session)
    session.add(
        MessageORM(
            id="msg-1",
            agent_id="agent-1",
            session_id="sess-1",
            role="user",
            content="x",
        )
    )
    session.commit()
    session.add(
        MessageEventORM(
            id="evt-1",
            agent_id="agent-1",
            session_id="sess-1",
            message_id="msg-1",
            event_type="delta",
            payload_json={},
            seq_no=1,
        )
    )
    session.commit()

    session.delete(session.get(MessageORM, "msg-1"))
    session.commit()
    evt = session.get(MessageEventORM, "evt-1")
    assert evt is not None
    assert evt.message_id is None  # SET NULL 生效


# ---------------------------------------------------------------------------
# AgentLockORM
# ---------------------------------------------------------------------------


def test_agent_lock_default_version(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    lock = AgentLockORM(agent_id="agent-1")
    session.add(lock)
    session.commit()
    fetched = session.get(AgentLockORM, "agent-1")
    assert fetched.lock_version == 0


def test_agent_lock_cascade_delete(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    session.add(AgentLockORM(agent_id="agent-1", lock_version=5))
    session.commit()
    session.delete(session.get(AgentORM, "agent-1"))
    session.commit()
    assert session.get(AgentLockORM, "agent-1") is None


# ---------------------------------------------------------------------------
# ModelORM
# ---------------------------------------------------------------------------


def test_model_orm_defaults(session: Session) -> None:
    m = ModelORM(id="m-1", name="gpt-x", provider="openai", api_key="sk-xxx")
    session.add(m)
    session.commit()

    fetched = session.get(ModelORM, "m-1")
    assert fetched.api_base_url is None
    assert fetched.enabled is True
    assert fetched.max_tokens == 4096
    # 注意源代码把 temperature 声明为 Integer 而非 Float,默认 7
    assert fetched.temperature == 7
    assert fetched.is_default is False


def test_model_orm_requires_api_key(session: Session) -> None:
    session.add(ModelORM(id="m-1", name="gpt-x", provider="openai"))
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# SkillRepositoryORM
# ---------------------------------------------------------------------------


def test_skill_repo_defaults(session: Session) -> None:
    repo = SkillRepositoryORM(repo_id="r-1", repo_name="r1", source_type="git")
    session.add(repo)
    session.commit()
    fetched = session.get(SkillRepositoryORM, "r-1")
    assert fetched.skill_discover_status == "init"
    assert fetched.skill_num == 0
    assert fetched.branch is None
    assert fetched.url is None
    assert fetched.local_path is None


# ---------------------------------------------------------------------------
# SkillORM
# ---------------------------------------------------------------------------


def test_skill_unique_repo_relative_path(session: Session) -> None:
    repo = SkillRepositoryORM(repo_id="r-1", repo_name="r1", source_type="git")
    session.add(repo)
    session.commit()
    session.add(
        SkillORM(skill_id="s-1", repo_id="r-1", skill_name="a", relative_path="x/y.md")
    )
    session.commit()

    session.add(
        SkillORM(skill_id="s-2", repo_id="r-1", skill_name="b", relative_path="x/y.md")
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_skill_cascade_delete_with_repo(session: Session) -> None:
    repo = SkillRepositoryORM(repo_id="r-1", repo_name="r1", source_type="git")
    session.add(repo)
    session.commit()
    session.add(
        SkillORM(skill_id="s-1", repo_id="r-1", skill_name="a", relative_path="a.md")
    )
    session.commit()

    session.delete(session.get(SkillRepositoryORM, "r-1"))
    session.commit()
    assert session.get(SkillORM, "s-1") is None


def test_skill_metadata_json_round_trip(session: Session) -> None:
    payload = {"version": 1, "tags": ["x"]}
    skill = SkillORM(
        skill_id="s-1",
        repo_id=None,
        skill_name="standalone",
        relative_path=None,
        metadata_json=payload,
    )
    session.add(skill)
    session.commit()
    session.expire_all()
    fetched = session.get(SkillORM, "s-1")
    assert fetched.metadata_json == payload


# ---------------------------------------------------------------------------
# AgentSkillORM
# ---------------------------------------------------------------------------


def test_agent_skill_builtin_requires_no_repo_id(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    session.add(
        AgentSkillORM(
            agent_id="agent-1",
            skill_id="s-1",
            source_type="builtin",
            skill_name="core",
        )
    )
    session.commit()


def test_agent_skill_builtin_with_repo_id_violates_check(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    repo = SkillRepositoryORM(repo_id="r-1", repo_name="r1", source_type="git")
    session.add(repo)
    session.commit()
    session.add(
        AgentSkillORM(
            agent_id="agent-1",
            skill_id="s-1",
            source_type="builtin",
            skill_name="core",
            repo_id="r-1",  # builtin 不允许带 repo_id
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_agent_skill_git_with_repo_id_ok(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    session.add(SkillRepositoryORM(repo_id="r-1", repo_name="r1", source_type="git"))
    session.commit()
    session.add(
        AgentSkillORM(
            agent_id="agent-1",
            skill_id="s-1",
            source_type="git",
            repo_id="r-1",
            skill_name="a",
        )
    )
    session.commit()


def test_agent_skill_wittyhub_without_repo_id_ok(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    session.add(
        AgentSkillORM(
            agent_id="agent-1",
            skill_id="s-1",
            source_type="wittyhub",
            skill_name="remote-skill",
        )
    )
    session.commit()


def test_agent_skill_clawhub_with_repo_id_ok(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    session.add(
        SkillRepositoryORM(repo_id="r-1", repo_name="r1", source_type="clawhub")
    )
    session.commit()
    session.add(
        AgentSkillORM(
            agent_id="agent-1",
            skill_id="s-1",
            source_type="clawhub",
            repo_id="r-1",
            skill_name="legacy-remote-skill",
        )
    )
    session.commit()


def test_agent_skill_wittyhub_with_repo_id_violates_check(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    session.add(SkillRepositoryORM(repo_id="r-1", repo_name="r1", source_type="git"))
    session.commit()
    session.add(
        AgentSkillORM(
            agent_id="agent-1",
            skill_id="s-1",
            source_type="wittyhub",
            repo_id="r-1",
            skill_name="remote-skill",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_agent_skill_clawhub_without_repo_id_violates_check(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    session.add(
        AgentSkillORM(
            agent_id="agent-1",
            skill_id="s-1",
            source_type="clawhub",
            skill_name="legacy-remote-skill",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_agent_skill_invalid_source_type_violates_check(session: Session) -> None:
    session.add(_new_agent())
    session.commit()
    session.add(
        AgentSkillORM(
            agent_id="agent-1",
            skill_id="s-1",
            source_type="bogus",
            skill_name="a",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_agent_skill_composite_primary_key(session: Session) -> None:
    """(agent_id, skill_id) 联合主键:同 agent 下不能重复装同一 skill。"""
    session.add(_new_agent())
    session.commit()
    session.add(
        AgentSkillORM(
            agent_id="agent-1",
            skill_id="s-1",
            source_type="builtin",
            skill_name="a",
        )
    )
    session.commit()

    session.add(
        AgentSkillORM(
            agent_id="agent-1",
            skill_id="s-1",
            source_type="builtin",
            skill_name="a-dup",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


# ---------------------------------------------------------------------------
# McpServerORM
# ---------------------------------------------------------------------------


def test_mcp_server_orm_persists_config(session: Session) -> None:
    cfg = {"command": "node", "args": ["x.js"], "env": {"FOO": "bar"}}
    s = McpServerORM(id="mcp-1", mcp_server_name="fs", mcp_server_config=cfg)
    session.add(s)
    session.commit()
    session.expire_all()
    fetched = session.get(McpServerORM, "mcp-1")
    assert fetched.mcp_server_config == cfg


def test_mcp_server_orm_updated_at_bumps(session: Session) -> None:
    import time

    s = McpServerORM(id="mcp-1", mcp_server_name="fs", mcp_server_config={})
    session.add(s)
    session.commit()
    session.expire_all()
    original = session.get(McpServerORM, "mcp-1").updated_at

    time.sleep(0.05)
    s.mcp_server_config = {"x": 1}
    session.commit()
    session.expire_all()
    refreshed = session.get(McpServerORM, "mcp-1")
    assert refreshed.updated_at > original
