from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from witty_service.domain.enums import AgentStatus
from witty_service.domain.errors import DomainError
from witty_service.persistence.db import (
    create_session_factory,
    create_sqlite_engine,
    init_db,
)
from witty_service.persistence.orm import (
    MessageEventORM,
    MessageORM,
    MessageStatus,
)
from witty_service.persistence.repositories import (
    AgentWithRuntimeStateRecord,
    SkillRecord,
    SqliteRepository,
)


@pytest.fixture()
def repo() -> SqliteRepository:
    engine = create_sqlite_engine("sqlite:///:memory:")
    init_db(engine, auto_create=True)
    factory = create_session_factory(engine)
    try:
        yield SqliteRepository(factory)
    finally:
        engine.dispose()


@pytest.fixture()
def session_factory() -> sessionmaker[Session]:
    engine = create_sqlite_engine("sqlite:///:memory:")
    init_db(engine, auto_create=True)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        engine.dispose()


def _create_agent(repo: SqliteRepository, agent_id: str = "agent-1") -> None:
    repo.create_agent_with_id(
        agent_id=agent_id,
        name="Demo Agent",
        description="demo",
        sandbox_type="local_process",
        adapter_type="http",
        workspace_path=f"/tmp/{agent_id}",
        idle_timeout_seconds=300,
        status=AgentStatus.running,
        mcp_server_list=["mcp-1"],
    )


def _create_session(
    repo: SqliteRepository,
    agent_id: str = "agent-1",
    session_id: str = "session-1",
) -> None:
    repo.upsert_session(
        session_id=session_id,
        agent_id=agent_id,
        status="idle",
        runtime_type="openclaw",
        runtime_session_key=f"agent:{agent_id}:session:{session_id}",
        remote_runtime_agent_id="runtime-agent-1",
    )


def test_agent_crud_and_recovery_filters(repo: SqliteRepository) -> None:
    _create_agent(repo, "running-agent")
    repo.create_agent_with_id(
        agent_id="deleted-agent",
        name="Deleted",
        sandbox_type="docker",
        adapter_type="http",
        workspace_path="/tmp/deleted",
        idle_timeout_seconds=60,
        status=AgentStatus.deleted,
    )

    agent = repo.get_agent("running-agent")

    assert agent is not None
    assert agent.name == "Demo Agent"
    assert agent.status is AgentStatus.running
    assert agent.mcp_server_list == ["mcp-1"]
    assert [item.id for item in repo.list_agents()] == ["running-agent"]

    updated = repo.update_agent_status("running-agent", AgentStatus.paused)
    repo.update_agent_mcp_server_list("running-agent", ["mcp-2", "mcp-3"])

    assert updated.status is AgentStatus.paused
    assert repo.get_agent("running-agent").mcp_server_list == [
        "mcp-2",
        "mcp-3",
    ]
    assert [item.id for item in repo.list_agents_needing_recovery()] == [
        "running-agent"
    ]
    assert repo.list_agents_needing_recovery(sandbox_type="docker") == []


def test_update_agent_status_raises_when_missing(
    repo: SqliteRepository,
) -> None:
    with pytest.raises(KeyError, match="Agent not found: missing"):
        repo.update_agent_status("missing", AgentStatus.running)


def test_session_upsert_list_update_and_delete(repo: SqliteRepository) -> None:
    _create_agent(repo)
    _create_session(repo)

    created = repo.get_session("session-1")
    updated = repo.upsert_session(
        session_id="session-1",
        agent_id="agent-1",
        status="running",
        runtime_type="openclaw",
        remote_runtime_agent_id=None,
    )
    metadata = repo.update_session_metadata(
        "session-1",
        title="Important chat",
        pinned=True,
    )

    assert created is not None
    assert created.runtime_type == "openclaw"
    assert created.runtime_session_key == "agent:agent-1:session:session-1"
    assert created.runtime_session_id is None
    assert updated.status == "running"
    assert updated.runtime_type == "openclaw"
    assert updated.remote_runtime_agent_id == "runtime-agent-1"
    assert metadata.title == "Important chat"
    assert metadata.pinned is True
    assert [item.id for item in repo.list_sessions("agent-1")] == ["session-1"]

    repo.delete_session("session-1")

    assert repo.get_session("session-1") is None


def test_sandbox_state_round_trip_and_handle(repo: SqliteRepository) -> None:
    _create_agent(repo)

    state = repo.save_sandbox_state(
        agent_id="agent-1",
        sandbox_payload_json={
            "sandbox_id": "sandbox-1",
            "agent_id": "agent-1",
            "workspace_path": "/tmp/agent-1",
            "metadata": {"port": 18080},
        },
        adapter_base_url="http://127.0.0.1:18080",
        adapter_ready=True,
    )
    fetched = repo.get_sandbox_state("agent-1")

    assert fetched == state
    assert fetched.handle.sandbox_id == "sandbox-1"
    assert fetched.handle.workspace_path == "/tmp/agent-1"
    assert fetched.handle.metadata == {"port": 18080}


def test_update_session_runtime_identity_persists_and_overwrites(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo)
    _create_session(repo, session_id="session-1")

    created = repo.update_session_runtime_identity(
        session_id="session-1",
        runtime_type="openclaw",
        runtime_session_id="runtime-session-1",
        runtime_session_key="agent:agent-1:session:session-1",
    )
    updated = repo.update_session_runtime_identity(
        session_id="session-1",
        runtime_type="openclaw",
        runtime_session_id="runtime-session-2",
        runtime_session_key="agent:agent-1:session:session-1",
    )

    assert created.runtime_type == "openclaw"
    assert created.runtime_session_id == "runtime-session-1"
    assert created.runtime_session_key == "agent:agent-1:session:session-1"
    assert updated.id == created.id
    assert updated.runtime_type == "openclaw"
    assert updated.runtime_session_id == "runtime-session-2"
    assert updated.runtime_session_key == "agent:agent-1:session:session-1"


def test_update_session_runtime_identity_requires_existing_session(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo)

    with pytest.raises(DomainError) as exc_info:
        repo.update_session_runtime_identity(
            session_id="missing-session",
            runtime_type="openclaw",
            runtime_session_id="runtime-session-1",
            runtime_session_key="agent:agent-1:session:missing-session",
        )

    assert exc_info.value.code == "SESSION_NOT_FOUND"
    assert exc_info.value.details["session_id"] == "missing-session"


def test_find_session_by_runtime_identity_returns_match(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo)
    _create_session(repo, session_id="session-1")
    repo.update_session_runtime_identity(
        session_id="session-1",
        runtime_type="openclaw",
        runtime_session_id="runtime-session-1",
        runtime_session_key="agent:agent-1:session:session-1",
    )

    result = repo.find_session_by_runtime_identity(
        runtime_type="openclaw",
        runtime_session_id="runtime-session-1",
    )

    assert result is not None
    assert result.id == "session-1"
    assert result.agent_id == "agent-1"


def test_find_session_by_runtime_identity_returns_none_when_missing(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo)
    _create_session(repo, session_id="session-1")

    result = repo.find_session_by_runtime_identity(
        runtime_type="openclaw",
        runtime_session_id="missing-runtime-session",
    )

    assert result is None


def test_list_sessions_by_runtime_session_ids_preserves_input_order(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo)
    _create_session(repo, session_id="session-1")
    _create_session(repo, session_id="session-2")
    repo.update_session_runtime_identity(
        session_id="session-1",
        runtime_type="openclaw",
        runtime_session_id="runtime-session-1",
        runtime_session_key="agent:agent-1:session:session-1",
    )
    repo.update_session_runtime_identity(
        session_id="session-2",
        runtime_type="openclaw",
        runtime_session_id="runtime-session-2",
        runtime_session_key="agent:agent-1:session:session-2",
    )

    result = repo.list_sessions_by_runtime_session_ids(
        runtime_type="openclaw",
        runtime_session_ids=[
            "runtime-session-2",
            "runtime-session-1",
            "runtime-session-2",
        ],
    )

    assert [item.id for item in result] == ["session-2", "session-1"]


def test_list_sessions_by_runtime_session_ids_ignores_missing_and_empty(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo)
    _create_session(repo, session_id="session-1")
    repo.update_session_runtime_identity(
        session_id="session-1",
        runtime_type="openclaw",
        runtime_session_id="runtime-session-1",
        runtime_session_key="agent:agent-1:session:session-1",
    )

    assert (
        repo.list_sessions_by_runtime_session_ids(
            runtime_type="openclaw",
            runtime_session_ids=[],
        )
        == []
    )

    result = repo.list_sessions_by_runtime_session_ids(
        runtime_type="openclaw",
        runtime_session_ids=["missing", "runtime-session-1"],
    )

    assert [item.id for item in result] == ["session-1"]


def test_list_runtime_session_ids_by_agent_id_filters_runtime_type_and_nulls(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo, "agent-1")
    _create_session(repo, agent_id="agent-1", session_id="session-1")
    _create_session(repo, agent_id="agent-1", session_id="session-2")
    _create_session(repo, agent_id="agent-1", session_id="session-3")
    repo.update_session_runtime_identity(
        session_id="session-1",
        runtime_type="openclaw",
        runtime_session_id="runtime-session-1",
        runtime_session_key="agent:agent-1:session:session-1",
    )
    repo.update_session_runtime_identity(
        session_id="session-3",
        runtime_type="other-runtime",
        runtime_session_id="other-runtime-session",
        runtime_session_key="agent:agent-1:session:session-3",
    )

    result = repo.list_runtime_session_ids_by_agent_id("agent-1")

    assert result == ["runtime-session-1"]


def test_list_agents_with_runtime_state_returns_outer_joined_records(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo, "agent-1")
    _create_agent(repo, "agent-2")
    repo.save_sandbox_state(
        agent_id="agent-1",
        sandbox_payload_json={
            "sandbox_id": "sandbox-1",
            "workspace_path": "/tmp/agent-1",
            "metadata": {"port": 18080},
        },
        adapter_base_url="http://127.0.0.1:18080",
        adapter_ready=True,
    )

    result = repo.list_agents_with_runtime_state()

    assert all(isinstance(item, AgentWithRuntimeStateRecord) for item in result)
    assert [item.agent.id for item in result] == ["agent-1", "agent-2"]
    assert result[0].runtime_state is not None
    assert result[0].runtime_state.adapter_base_url == "http://127.0.0.1:18080"
    assert result[1].runtime_state is None


def test_list_agent_records_by_ids_preserves_input_order(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo, "agent-1")
    _create_agent(repo, "agent-2")
    repo.create_agent_with_id(
        agent_id="agent-3",
        name="Deleted Agent",
        description="demo",
        sandbox_type="local_process",
        adapter_type="http",
        workspace_path="/tmp/agent-3",
        idle_timeout_seconds=300,
        status=AgentStatus.deleted,
    )

    result = repo.list_agent_records_by_ids(
        ["agent-2", "missing-agent", "agent-1", "agent-2", "agent-3"]
    )

    assert [item.id for item in result] == ["agent-2", "agent-1"]


def test_message_events_retry_and_summary_methods(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo)
    _create_session(repo)
    user_message_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="user",
        content="hello",
    )
    assistant_message_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="partial",
        status=MessageStatus.generating,
    )

    first_event_id, first_seq = repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=assistant_message_id,
        event_type="thinking",
        payload_json={"thinking": "plan"},
        seq_no=1,
    )
    second_event_id, second_seq = repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=assistant_message_id,
        event_type="usage.updated",
        payload_json={
            "input_tokens": 1,
            "output_tokens": 2,
            "total_cost": 0.1,
        },
        seq_no=1,
    )

    assert first_seq == 1
    assert second_seq == 2
    assert first_event_id != second_event_id
    assert repo.get_message_count("session-1") == 2
    assert repo.get_first_user_message("session-1") == "hello"
    assert repo.get_last_assistant_status("session-1") == "generating"

    repo.update_message_content(assistant_message_id, "done")
    repo.update_message_status(assistant_message_id, MessageStatus.completed)
    messages, has_more = repo.get_messages_with_events("session-1", limit=10)

    assert has_more is False
    assert [item["id"] for item in messages] == [
        user_message_id,
        assistant_message_id,
    ]
    assert messages[1]["content"] == "done"
    assert messages[1]["status"] == "completed"
    assert messages[1]["thinking"] == ["plan"]
    assert messages[1]["usage"] == {
        "inputTokens": 1,
        "outputTokens": 2,
        "totalCost": 0.1,
    }


def test_stale_generating_messages_and_compaction(
    session_factory: sessionmaker[Session],
) -> None:
    repo = SqliteRepository(session_factory)
    _create_agent(repo)
    _create_session(repo)
    message_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="streaming",
        status=MessageStatus.generating,
    )
    repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=message_id,
        event_type="message.delta",
        payload_json={"delta": "a"},
        seq_no=1,
    )
    repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=message_id,
        event_type="thinking",
        payload_json={"thinking": "keep"},
        seq_no=2,
    )

    with session_factory() as session:
        row = session.get(MessageORM, message_id)
        row.last_stream_at = datetime.now(UTC) - timedelta(
            seconds=120,
        )
        session.commit()

    stale = repo.find_stale_generating_messages(stale_threshold_seconds=60)
    repo.compact_message_delta_events(message_id)

    assert [item.id for item in stale] == [message_id]
    with session_factory() as session:
        rows = session.query(MessageEventORM).order_by(MessageEventORM.seq_no).all()
        event_types = [item.event_type for item in rows]
        payloads = [dict(item.payload_json or {}) for item in rows]
    # 正文增量保留在时间线上（压缩为一条），不再整段删除
    assert event_types == ["message.delta", "thinking"]
    assert payloads == [{"delta": "a"}, {"thinking": "keep"}]


def test_compact_message_delta_events_keeps_timeline_order(
    repo: SqliteRepository,
) -> None:
    """压缩后正文分段仍留在原时间线位置：正文/思考/正文 的交替顺序不丢。"""
    _create_agent(repo)
    _create_session(repo)
    message_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="第一段第二段",
        status=MessageStatus.completed,
    )
    events: list[tuple[str, dict[str, Any]]] = [
        ("message.delta", {"delta": "第一"}),
        ("message.delta", {"delta": "段"}),
        ("thinking", {"thinking": "思考"}),
        ("tool.call.delta", {"delta": '{"pa":'}),
        ("message.delta", {"delta": "第二段"}),
    ]
    for index, (event_type, payload) in enumerate(events, start=1):
        repo.create_message_event_with_retry(
            agent_id="agent-1",
            session_id="session-1",
            message_id=message_id,
            event_type=event_type,
            payload_json=payload,
            seq_no=index,
        )

    repo.compact_message_delta_events(message_id)

    messages, _ = repo.get_messages_with_events("session-1")
    assembled = messages[0]["events"]
    # tool.call.delta 删除，其余事件保持原顺序，同段增量合并
    assert [item["type"] for item in assembled] == [
        "message.delta",
        "thinking",
        "message.delta",
    ]
    assert [item["content"] for item in assembled] == ["第一段", "思考", "第二段"]


def test_compact_message_delta_events_thinking_delta_dedupe(
    repo: SqliteRepository,
) -> None:
    """thinking.delta 被完整 thinking 事件覆盖时删除，否则就地转成 thinking。"""
    _create_agent(repo)
    _create_session(repo)
    message_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="正文",
        status=MessageStatus.completed,
    )
    events: list[tuple[str, dict[str, Any]]] = [
        ("thinking.delta", {"delta": "覆盖"}),
        ("thinking.delta", {"delta": "重复"}),
        ("thinking", {"thinking": "覆盖重复"}),
        ("thinking.delta", {"delta": "独有思考"}),
        ("message.delta", {"delta": "正文"}),
    ]
    for index, (event_type, payload) in enumerate(events, start=1):
        repo.create_message_event_with_retry(
            agent_id="agent-1",
            session_id="session-1",
            message_id=message_id,
            event_type=event_type,
            payload_json=payload,
            seq_no=index,
        )

    repo.compact_message_delta_events(message_id)

    messages, _ = repo.get_messages_with_events("session-1")
    assembled = messages[0]["events"]
    assert [item["type"] for item in assembled] == [
        "thinking",
        "thinking",
        "message.delta",
    ]
    assert [item["content"] for item in assembled] == [
        "覆盖重复",
        "独有思考",
        "正文",
    ]
    assert messages[0]["thinking"] == ["覆盖重复", "独有思考"]


def test_assemble_message_appends_missing_delta_tail(repo: SqliteRepository) -> None:
    """增量只是正文前缀时补齐尾巴，避免刷新后正文缺失。"""
    _create_agent(repo)
    _create_session(repo)
    message_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="Hello world",
        status=MessageStatus.completed,
    )
    repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=message_id,
        event_type="message.delta",
        payload_json={"delta": "Hello"},
        seq_no=1,
    )

    messages, _ = repo.get_messages_with_events("session-1")
    assembled = messages[0]["events"]
    assert [item["type"] for item in assembled] == ["message.delta"]
    assert assembled[0]["content"] == "Hello world"


def test_assemble_message_falls_back_when_deltas_mismatch(
    repo: SqliteRepository,
) -> None:
    """增量与正文完全对不上时回退为整段正文，避免正文丢失或重复。"""
    _create_agent(repo)
    _create_session(repo)
    message_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="完整正文",
        status=MessageStatus.completed,
    )
    repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=message_id,
        event_type="message.delta",
        payload_json={"delta": "截断内容"},
        seq_no=1,
    )

    messages, _ = repo.get_messages_with_events("session-1")
    assembled = messages[0]["events"]
    assert [item["type"] for item in assembled] == ["message.delta"]
    assert [item["content"] for item in assembled] == ["完整正文"]


def test_assemble_message_legacy_message_without_deltas(
    repo: SqliteRepository,
) -> None:
    """老数据（增量已被整段删除）仍把正文挂到时间线末尾，不出现空消息。"""
    _create_agent(repo)
    _create_session(repo)
    message_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="历史正文",
        status=MessageStatus.completed,
    )
    repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=message_id,
        event_type="thinking",
        payload_json={"thinking": "旧思考"},
        seq_no=1,
    )

    messages, _ = repo.get_messages_with_events("session-1")
    assembled = messages[0]["events"]
    assert [item["type"] for item in assembled] == ["thinking", "message.delta"]
    assert assembled[1]["content"] == "历史正文"


# ---------------------------------------------------------------------------
# 分段一致性：压缩前 / 压缩后 / 重复压缩 必须给出同一份时间线
#
# 背景：分段边界必须只由「会渲染的事件」决定。tool.call.delta（前端不消费）与空
# delta 若被当作边界，压缩把它们删掉后分段就会变，用户会看到布局在压缩前后跳变。
# ---------------------------------------------------------------------------


def _timeline(repo: SqliteRepository, session_id: str, message_id: str) -> list[str]:
    """把时间线压成可断言的字符串列表：正文/思考各自合并为一段。"""
    messages, _ = repo.get_messages_with_events(session_id)
    target = next(item for item in messages if item["id"] == message_id)
    timeline: list[str] = []
    for event in target["events"]:
        kind = event.get("type")
        if kind not in ("message.delta", "thinking"):
            timeline.append(str(kind))
            continue
        label = "delta" if kind == "message.delta" else "think"
        text = event.get("content") or ""
        if timeline and timeline[-1].startswith(f"{label}("):
            timeline[-1] = f"{timeline[-1][:-1]}{text})"
        else:
            timeline.append(f"{label}({text})")
    return timeline


def _seed_message(
    repo: SqliteRepository,
    *,
    content: str,
    events: list[tuple[str, dict[str, Any]]],
    suffix: str,
) -> tuple[str, str]:
    _create_agent(repo)
    session_id = f"session-{suffix}"
    repo.upsert_session(
        session_id=session_id,
        agent_id="agent-1",
        status="idle",
        runtime_type="openclaw",
        runtime_session_key=f"agent:agent-1:session:{session_id}",
        remote_runtime_agent_id="runtime-agent-1",
    )
    message_id = repo.create_message(
        agent_id="agent-1",
        session_id=session_id,
        role="assistant",
        content=content,
        status=MessageStatus.completed,
    )
    for index, (event_type, payload) in enumerate(events, start=1):
        repo.create_message_event_with_retry(
            agent_id="agent-1",
            session_id=session_id,
            message_id=message_id,
            event_type=event_type,
            payload_json=payload,
            seq_no=index,
        )
    return session_id, message_id


# 与前端 buildMessageEventGroups 的实际行为对齐的共享向量（正文/思考/工具增量）
TIMELINE_VECTORS: list[
    tuple[str, str, str, list[tuple[str, dict[str, Any]]], list[str]]
] = [
    (
        "v1-merge-adjacent",
        "普通多段合并",
        "AB",
        [("message.delta", {"delta": "A"}), ("message.delta", {"delta": "B"})],
        ["delta(AB)"],
    ),
    (
        "v2-empty-delta",
        "空 delta 夹在中间仍算同一段",
        "AB",
        [
            ("message.delta", {"delta": "A"}),
            ("message.delta", {"delta": ""}),
            ("message.delta", {"delta": "B"}),
        ],
        ["delta(AB)"],
    ),
    (
        "v3-tool-call-delta",
        "tool.call.delta 夹在中间仍算同一段",
        "AB",
        [
            ("message.delta", {"delta": "A"}),
            ("tool.call.delta", {"content": "stdout..."}),
            ("message.delta", {"delta": "B"}),
        ],
        ["delta(AB)"],
    ),
    (
        "v4-thinking-boundary",
        "思考段是真正的分段边界",
        "AB",
        [
            ("message.delta", {"delta": "A"}),
            ("thinking", {"thinking": "想"}),
            ("message.delta", {"delta": "B"}),
        ],
        ["delta(A)", "think(想)", "delta(B)"],
    ),
    (
        "v5-merge-thinking-run",
        "连续思考合并为一段",
        "AB",
        [
            ("message.delta", {"delta": "A"}),
            ("thinking", {"thinking": "想"}),
            ("thinking", {"thinking": "想2"}),
            ("message.delta", {"delta": "B"}),
        ],
        ["delta(A)", "think(想想2)", "delta(B)"],
    ),
]


@pytest.mark.parametrize(
    ("suffix", "label", "content", "events", "expected"),
    TIMELINE_VECTORS,
    ids=[vector[0] for vector in TIMELINE_VECTORS],
)
def test_compact_is_idempotent_and_preserves_timeline(
    repo: SqliteRepository,
    suffix: str,
    label: str,
    content: str,
    events: list[tuple[str, dict[str, Any]]],
    expected: list[str],
) -> None:
    """压缩 N 次的结果必须一致，且等于压缩前的读侧装配结果。"""
    session_id, message_id = _seed_message(
        repo, content=content, events=events, suffix=suffix
    )

    before = _timeline(repo, session_id, message_id)
    assert before == expected, f"{label}: 压缩前装配与预期不符"

    repo.compact_message_delta_events(message_id)
    after_first = _timeline(repo, session_id, message_id)
    assert after_first == expected, f"{label}: 压缩后时间线发生变化"

    repo.compact_message_delta_events(message_id)
    repo.compact_message_delta_events(message_id)
    assert _timeline(repo, session_id, message_id) == expected, f"{label}: 压缩不幂等"


@pytest.mark.parametrize(
    ("suffix", "label", "content", "events", "expected"),
    TIMELINE_VECTORS,
    ids=[vector[0] for vector in TIMELINE_VECTORS],
)
def test_compact_and_assemble_agree(
    repo: SqliteRepository,
    suffix: str,
    label: str,
    content: str,
    events: list[tuple[str, dict[str, Any]]],
    expected: list[str],
) -> None:
    """读侧装配与压缩必须给出同一份时间线（压缩前 vs 压缩后）。"""
    session_id, message_id = _seed_message(
        repo, content=content, events=events, suffix=suffix
    )

    assembled_before = _timeline(repo, session_id, message_id)
    repo.compact_message_delta_events(message_id)
    assembled_after = _timeline(repo, session_id, message_id)

    assert assembled_before == assembled_after, (
        f"{label}: 压缩改变了时间线布局 {assembled_before} -> {assembled_after}"
    )


def test_assemble_message_delta_lands_in_events(repo: SqliteRepository) -> None:
    """正文分段必须落在 events 里。

    回归：曾经只把分段累积进 delta_items、忘了 event_items.append，导致刷新后
    events 里一个 message.delta 都没有，前端只能回退到「整段正文挂末尾」，
    正文与思考/工具调用的交替顺序全部丢失。
    """
    session_id, message_id = _seed_message(
        repo,
        content="第一段第二段",
        events=[
            ("message.delta", {"delta": "第一段"}),
            ("thinking", {"thinking": "思考"}),
            ("message.delta", {"delta": "第二段"}),
        ],
        suffix="v6-lands-in-events",
    )

    messages, _ = repo.get_messages_with_events(session_id)
    target = next(item for item in messages if item["id"] == message_id)
    deltas = [item for item in target["events"] if item["type"] == "message.delta"]

    assert [item["content"] for item in deltas] == ["第一段", "第二段"]
    # 时间线顺序：正文段在思考之前，第二段在思考之后
    assert [item["type"] for item in target["events"]] == [
        "message.delta",
        "thinking",
        "message.delta",
    ]


def test_assemble_message_does_not_duplicate_trailing_delta(
    repo: SqliteRepository,
) -> None:
    """分段已能还原完整正文时，不得再追加一条整段正文事件。

    回归：对账分支在「增量之和 == content」时也会走到兜底追加，多出一个
    content 为空的 message.delta 块。
    """
    session_id, message_id = _seed_message(
        repo,
        content="AB",
        events=[("message.delta", {"delta": "A"}), ("message.delta", {"delta": "B"})],
        suffix="v7-no-duplicate",
    )

    messages, _ = repo.get_messages_with_events(session_id)
    target = next(item for item in messages if item["id"] == message_id)
    deltas = [item for item in target["events"] if item["type"] == "message.delta"]

    assert len(deltas) == 1
    assert deltas[0]["content"] == "AB"


def test_model_and_mcp_server_crud(repo: SqliteRepository) -> None:
    model = repo.create_model_with_id(
        model_id="model-1",
        name="GPT",
        provider="openai",
        api_key="secret",
        api_base_url="https://api.example.com",
        is_default=True,
    )
    server = repo.create_mcp_server_with_id(
        server_id="mcp-1",
        mcp_server_name="fs",
        mcp_server_config={"fs": {"command": "npx"}},
    )

    updated_model = repo.update_model(
        "model-1",
        name="GPT 4.1",
        enabled=False,
        temperature=0.2,
    )
    updated_server = repo.update_mcp_server(
        "mcp-1",
        mcp_server_name="filesystem",
        mcp_server_config={"filesystem": {"command": "node"}},
    )

    assert model.id == "model-1"
    assert [item.id for item in repo.list_models()] == ["model-1"]
    assert updated_model.name == "GPT 4.1"
    assert updated_model.enabled is False
    assert updated_model.temperature == 0.2
    assert server.id == "mcp-1"
    assert [item.id for item in repo.list_mcp_servers()] == ["mcp-1"]
    assert updated_server.mcp_server_name == "filesystem"

    repo.delete_model("model-1")
    repo.delete_mcp_server("mcp-1")

    assert repo.get_model("model-1") is None
    assert repo.get_mcp_server("mcp-1") is None


def test_skill_repository_and_skills_lifecycle(repo: SqliteRepository) -> None:
    repository = repo.create_skill_repository(
        name="https://github.com/example/skills@main",
        source_type="git",
        branch="main",
        url="https://github.com/example/skills",
        local_path="/tmp/skills",
        skill_discover_status="init",
    )
    skill = SkillRecord(
        skill_id="skill-1",
        repo_id=repository.repo_id,
        skill_name="terminal-helper",
        relative_path="skills/terminal-helper/SKILL.md",
        metadata={"title": "Terminal Helper"},
        skill_source="git",
        skill_md_url="https://example.com/SKILL.md",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    repo.update_skills(repository.repo_id, [skill])
    updated_repository = repo.update_skill_repository(
        repository.repo_id,
        skill_discover_status="done",
        skill_num=1,
    )

    assert updated_repository.skill_discover_status == "done"
    assert (
        repo.get_skill_repository_by_name(repository.repo_name).repo_id
        == repository.repo_id
    )
    assert [item.skill_id for item in repo.list_skills()] == ["skill-1"]
    fetched_skill = repo.get_skill_by_skill_id("skill-1")
    assert fetched_skill.metadata == {"title": "Terminal Helper"}

    repo.delete_skill_repository(repository.repo_id)

    assert repo.get_skill_repository(repository.repo_id) is None
    assert repo.list_skills() == []


def test_builtin_and_installed_agent_skills_lifecycle(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo)
    builtin = repo.upsert_builtin_skill(
        skill_id="builtin-1",
        skill_name="Builtin Skill",
        metadata={"source": "runtime"},
        skill_source="runtime",
        relative_path="/skills/builtin.md",
    )
    installed = repo.upsert_installed_agent_skill(
        agent_id="agent-1",
        skill_id=builtin.skill_id,
        source_type="builtin",
        skill_name=builtin.skill_name,
        metadata=builtin.metadata,
        skill_source=builtin.skill_source,
    )

    assert installed.source_type == "builtin"
    assert (
        repo.get_installed_agent_skill(
            agent_id="agent-1",
            skill_id="builtin-1",
        )
        is not None
    )
    assert [item.skill_id for item in repo.list_installed_agent_skills("agent-1")] == [
        "builtin-1"
    ]

    repo.delete_installed_agent_skill(
        agent_id="agent-1",
        skill_id="builtin-1",
    )

    assert (
        repo.get_installed_agent_skill(
            agent_id="agent-1",
            skill_id="builtin-1",
        )
        is None
    )
    assert repo.get_skill_by_skill_id("builtin-1") is None


def test_replace_installed_agent_skills_from_runtime_normalizes_snapshot(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo)
    repo.replace_installed_agent_skills_from_runtime(
        agent_id="agent-1",
        skills=[
            {
                "name": "Read",
                "source": "runtime",
                "filePath": "/skills/read.md",
            },
            {"name": "Read", "source": "duplicate", "filePath": "/skills/read.md"},
            {"name": "  Write  ", "source": "runtime"},
            {"source": "invalid"},
        ],
    )

    installed = repo.list_installed_agent_skills("agent-1")

    assert [item.skill_name for item in installed] == ["Read", "Write"]
    assert installed[0].source_type == "builtin"
    assert installed[0].relative_path == "/skills/read.md"

    repo.replace_installed_agent_skills_from_runtime(
        agent_id="agent-1",
        skills=[{"name": "Write", "source": "runtime"}],
    )

    assert [
        item.skill_name for item in repo.list_installed_agent_skills("agent-1")
    ] == ["Write"]


def test_replace_installed_agent_skills_from_runtime_preserves_non_builtin_records(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo)
    repository = repo.create_skill_repository(
        name="https://github.com/example/skills@main",
        source_type="git",
        branch="main",
        url="https://github.com/example/skills",
        local_path="/tmp/skills",
        skill_discover_status="done",
    )
    repo.upsert_installed_agent_skill(
        agent_id="agent-1",
        skill_id="git-1",
        source_type="git",
        repo_id=repository.repo_id,
        skill_name="Repository Skill",
    )
    repo.upsert_installed_agent_skill(
        agent_id="agent-1",
        skill_id="wittyhub-1",
        source_type="wittyhub",
        repo_id=None,
        skill_name="Witty Skill",
        relative_path="  /skills/witty/SKILL.md  ",
        skill_source="https://gitcode.com/example/repo",
    )

    repo.replace_installed_agent_skills_from_runtime(
        agent_id="agent-1",
        skills=[
            {
                "name": "Witty Skill",
                "source": "runtime",
                "filePath": "/skills/witty/SKILL.md",
            },
            {"name": "Builtin Skill", "source": "runtime"},
        ],
    )

    installed = repo.list_installed_agent_skills("agent-1")
    assert [item.skill_name for item in installed] == [
        "Repository Skill",
        "Witty Skill",
        "Builtin Skill",
    ]
    assert [item.source_type for item in installed] == ["git", "wittyhub", "builtin"]
    assert [
        item.skill_id for item in installed if item.skill_name == "Witty Skill"
    ] == ["wittyhub-1"]


def test_repository_summary_queries_and_agent_delete(repo: SqliteRepository) -> None:
    _create_agent(repo)
    _create_session(repo, session_id="session-1")
    _create_session(repo, session_id="session-2")
    repo.update_session_metadata("session-1", title="Pinned", pinned=True)
    repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="user",
        content="hello world",
    )
    repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="hi",
        status=MessageStatus.completed,
    )

    sessions = repo.list_sessions_with_summary("agent-1")
    agents = repo.list_agents_with_conversations()

    assert [item["id"] for item in sessions] == ["session-1", "session-2"]
    assert sessions[0]["title"] == "Pinned"
    assert sessions[0]["message_count"] == 2
    assert sessions[0]["last_message_status"] == "completed"
    assert agents[0]["id"] == "agent-1"
    assert [item["id"] for item in agents[0]["conversations"]] == [
        "session-1",
        "session-2",
    ]

    repo.delete_agent("agent-1")
    repo.delete_agent("missing")

    assert repo.get_agent("agent-1") is None
    assert repo.list_agents_with_conversations() == []


def test_messages_with_events_pagination_and_tool_calls(repo: SqliteRepository) -> None:
    _create_agent(repo)
    _create_session(repo)
    first_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="first",
        status=MessageStatus.completed,
    )
    second_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="second",
        status=MessageStatus.generating,
    )
    repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=second_id,
        event_type="tool.call.started",
        payload_json={
            "tool_call_id": "tool-1",
            "tool_name": "shell",
            "arguments": {"cmd": "echo hi"},
        },
        seq_no=1,
    )
    repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=second_id,
        event_type="tool.call.response",
        payload_json={
            "tool_call_id": "tool-1",
            "content": "hi",
            "duration": 12,
            "is_error": False,
        },
        seq_no=2,
    )
    first_page, has_more = repo.get_messages_with_events("session-1", limit=1)
    before = first_page[0]["timestamp"].replace("Z", "+00:00")
    empty_page, empty_has_more = repo.get_messages_with_events(
        "session-1",
        limit=10,
        before=before,
    )

    assert has_more is True
    assert first_page[0]["id"] == second_id
    assert first_page[0]["isStreaming"] is True
    assert first_page[0]["toolCalls"] == [
        {
            "id": "tool-1",
            "name": "shell",
            "status": "completed",
            "input": {"cmd": "echo hi"},
            "output": "hi",
            "duration": 12,
        }
    ]
    assert empty_page[0]["id"] == first_id
    assert empty_has_more is False


def test_create_assistant_message_and_stream_updates(repo: SqliteRepository) -> None:
    _create_agent(repo)
    _create_session(repo)
    event_id, seq_no = repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=None,
        event_type="message.delta",
        payload_json={"delta": "hello"},
        seq_no=1,
    )

    message_id = repo.create_assistant_message_and_bind_events(
        agent_id="agent-1",
        session_id="session-1",
        content="hello",
        event_ids=[event_id],
    )
    repo.update_message_stream_at(message_id)
    messages, _ = repo.get_messages_with_events("session-1")

    assert seq_no == 1
    assert messages[0]["id"] == message_id
    assert messages[0]["content"] == "hello"


def test_create_message_events_bulk_writes_batch_in_one_commit(
    repo: SqliteRepository,
) -> None:
    """批量落库：一次写入整批事件，seq_no 按传入顺序保留。"""
    _create_agent(repo)
    _create_session(repo)
    message_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="",
        status=MessageStatus.generating,
    )

    written = repo.create_message_events_bulk(
        agent_id="agent-1",
        session_id="session-1",
        message_id=message_id,
        events=[
            (1, "thinking.delta", {"delta": "a"}),
            (2, "message.delta", {"delta": "b"}),
            (3, "message.delta", {"delta": "c"}),
        ],
    )

    assert written == 3
    with repo._session_factory() as db:  # noqa: SLF001 - 断言原始行，绕开组装/压缩
        rows = (
            db.query(MessageEventORM)
            .filter(MessageEventORM.session_id == "session-1")
            .order_by(MessageEventORM.seq_no)
            .all()
        )
    assert [(row.seq_no, row.event_type) for row in rows] == [
        (1, "thinking.delta"),
        (2, "message.delta"),
        (3, "message.delta"),
    ]
    assert all(row.message_id == message_id for row in rows)


def test_create_message_events_bulk_retries_on_seq_conflict(
    repo: SqliteRepository,
) -> None:
    """seq_no 撞车（并发写）时整批平移重试，不丢事件。"""
    _create_agent(repo)
    _create_session(repo)
    repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        event_type="message.delta",
        payload_json={"delta": "已有"},
        seq_no=1,
    )

    written = repo.create_message_events_bulk(
        agent_id="agent-1",
        session_id="session-1",
        events=[
            (1, "message.delta", {"delta": "撞车-a"}),
            (2, "message.delta", {"delta": "撞车-b"}),
        ],
    )

    assert written == 2
    with repo._session_factory() as db:  # noqa: SLF001
        rows = (
            db.query(MessageEventORM)
            .filter(MessageEventORM.session_id == "session-1")
            .order_by(MessageEventORM.seq_no)
            .all()
        )
    # 冲突批整批平移到已有最大 seq_no 之后，原有事件不受影响
    assert [row.seq_no for row in rows] == [1, 2, 3]
    assert [row.payload_json["delta"] for row in rows] == [
        "已有",
        "撞车-a",
        "撞车-b",
    ]


# ---------------------------------------------------------------------------
# _assemble_message question 事件处理
# ---------------------------------------------------------------------------


def test_assemble_message_question_events(repo: SqliteRepository) -> None:
    """question.asked + question.replied + question.rejected 事件正确组装。"""
    _create_agent(repo)
    _create_session(repo)
    message_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="Let me ask you something",
        status=MessageStatus.completed,
    )
    repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=message_id,
        event_type="question.asked",
        payload_json={
            "question_id": "que_001",
            "questions": [
                {
                    "question": "Which file?",
                    "header": "File",
                    "options": [{"label": "a.md", "description": "markdown"}],
                }
            ],
            "tool": {"messageID": "msg-x", "callID": "call-x"},
        },
        seq_no=1,
    )
    # question.replied 使用 request_id 而非 question_id
    repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=message_id,
        event_type="question.replied",
        payload_json={
            "request_id": "que_001",
            "answers": [["a.md"]],
        },
        seq_no=2,
    )
    # 第二个问题: 仅 asked + rejected（无 replied）
    repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=message_id,
        event_type="question.asked",
        payload_json={
            "question_id": "que_002",
            "questions": [{"question": "Proceed?", "header": "Confirm"}],
        },
        seq_no=3,
    )
    repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=message_id,
        event_type="question.rejected",
        payload_json={
            "request_id": "que_002",
        },
        seq_no=4,
    )

    messages, _ = repo.get_messages_with_events("session-1")

    msg = messages[0]
    # questionId / questionStatus 应为最后一个 question 事件的状态
    assert msg["questionId"] == "que_002"
    assert msg["questionStatus"] == "rejected"
    # question.asked 会重置 question_answers，que_002 是 rejected 状态，不应有 answers
    assert "questionAnswers" not in msg
    # question 列表来自最后一个 question.asked（最后一个有 questions 的事件赋值）
    assert msg["question"] == [{"question": "Proceed?", "header": "Confirm"}]

    # 检查每个 event 的 item
    events = msg["events"]
    asked_1 = events[0]
    assert asked_1["type"] == "question.asked"
    assert asked_1["payload"] == {
        "question_id": "que_001",
        "questions": [
            {
                "question": "Which file?",
                "header": "File",
                "options": [{"label": "a.md", "description": "markdown"}],
            }
        ],
    }

    replied = events[1]
    assert replied["type"] == "question.replied"
    assert replied["payload"] == {
        "question_id": "que_001",
        "answers": [["a.md"]],
    }

    asked_2 = events[2]
    assert asked_2["type"] == "question.asked"
    assert asked_2["payload"]["question_id"] == "que_002"

    rejected = events[3]
    assert rejected["type"] == "question.rejected"
    assert rejected["payload"] == {"question_id": "que_002"}


def test_assemble_message_question_standalone_replied(repo: SqliteRepository) -> None:
    """仅有 question.replied（无先前的 question.asked）时也能正确获取 question_id。"""
    _create_agent(repo)
    _create_session(repo)
    message_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="answer recorded",
        status=MessageStatus.completed,
    )
    repo.create_message_event_with_retry(
        agent_id="agent-1",
        session_id="session-1",
        message_id=message_id,
        event_type="question.replied",
        payload_json={
            "request_id": "que_solo",
            "answers": [["yes"]],
        },
        seq_no=1,
    )

    messages, _ = repo.get_messages_with_events("session-1")
    msg = messages[0]

    assert msg["questionId"] == "que_solo"
    assert msg["questionStatus"] == "replied"
    assert msg["questionAnswers"] == [["yes"]]
    # 无 question.asked 时不应有 question 字段
    assert "question" not in msg

    replied_event = msg["events"][0]
    assert replied_event["type"] == "question.replied"
    assert replied_event["payload"] == {
        "question_id": "que_solo",
        "answers": [["yes"]],
    }


# ---------------------------------------------------------------------------
# _assemble_message artifact 事件处理
# ---------------------------------------------------------------------------


def _artifact_payload(
    path: str, *, status: str, content: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": path,
        "name": path.rsplit("/", 1)[-1],
        "type": "html" if path.endswith(".html") else "image",
        "status": status,
        "version": 1,
        "relative_path": path,
        "size": len(content) if content is not None else None,
        "mime": "text/html" if path.endswith(".html") else "image/png",
    }
    if content is not None:
        payload["content"] = content
    return payload


def _create_artifact_message(
    repo: SqliteRepository,
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    _create_agent(repo)
    _create_session(repo)
    message_id = repo.create_message(
        agent_id="agent-1",
        session_id="session-1",
        role="assistant",
        content="done",
        status=MessageStatus.completed,
    )
    for seq_no, (event_type, payload) in enumerate(events, start=1):
        repo.create_message_event_with_retry(
            agent_id="agent-1",
            session_id="session-1",
            message_id=message_id,
            event_type=event_type,
            payload_json=payload,
            seq_no=seq_no,
        )


def test_assemble_message_artifact_events_aggregated_by_id(
    repo: SqliteRepository,
) -> None:
    """artifact.started + artifact.completed 聚合为消息 artifacts 字段（camelCase）。"""
    _create_artifact_message(
        repo,
        [
            (
                "artifact.started",
                _artifact_payload("output/demo.html", status="creating"),
            ),
            (
                "artifact.completed",
                _artifact_payload(
                    "output/demo.html", status="ready", content="<h1>hi</h1>"
                ),
            ),
        ],
    )

    messages, _ = repo.get_messages_with_events("session-1")
    msg = messages[0]

    assert msg["artifacts"] == [
        {
            "id": "output/demo.html",
            "name": "demo.html",
            "type": "html",
            "status": "ready",
            "version": 1,
            "relativePath": "output/demo.html",
            "size": len("<h1>hi</h1>"),
            "mime": "text/html",
            "content": "<h1>hi</h1>",
        }
    ]
    started_item = msg["events"][0]
    assert started_item["type"] == "artifact.started"
    assert started_item["artifact"]["status"] == "creating"
    assert started_item["artifact"]["relativePath"] == "output/demo.html"


def test_assemble_message_artifact_error_keeps_error_status(
    repo: SqliteRepository,
) -> None:
    """write 失败的 artifact.completed（error）保留 error 状态且无 content。"""
    _create_artifact_message(
        repo,
        [
            (
                "artifact.started",
                _artifact_payload("output/logo.png", status="creating"),
            ),
            (
                "artifact.completed",
                _artifact_payload("output/logo.png", status="error"),
            ),
        ],
    )

    messages, _ = repo.get_messages_with_events("session-1")
    msg = messages[0]

    assert msg["artifacts"][0]["status"] == "error"
    assert "content" not in msg["artifacts"][0]
