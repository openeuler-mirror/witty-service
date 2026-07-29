from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
        row.last_stream_at = datetime.now(timezone.utc) - timedelta(
            seconds=120,
        )
        session.commit()

    stale = repo.find_stale_generating_messages(stale_threshold_seconds=60)
    repo.compact_message_delta_events(message_id)

    assert [item.id for item in stale] == [message_id]
    with session_factory() as session:
        event_types = [
            item.event_type
            for item in session.query(MessageEventORM)
            .order_by(MessageEventORM.seq_no)
            .all()
        ]
    assert event_types == ["thinking"]


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
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
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
    assert [
        item.skill_id
        for item in repo.list_installed_agent_skills("agent-1")
    ] == ["builtin-1"]

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
    assert [item.skill_id for item in installed if item.skill_name == "Witty Skill"] == [
        "wittyhub-1"
    ]


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
    # questionAnswers 来自 question.replied
    assert msg["questionAnswers"] == [["a.md"]]
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
