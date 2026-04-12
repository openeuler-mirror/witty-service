from __future__ import annotations

from pathlib import Path

from witty_service.persistence.db import create_session_factory, create_sqlite_engine, init_db
from witty_service.persistence.orm import MessageEventORM, MessageORM
from witty_service.persistence.repositories import SqliteRepository
from witty_service.sse.relay import relay_and_persist


def build_repository(db_path: Path) -> SqliteRepository:
    engine = create_sqlite_engine(f"sqlite:///{db_path}")
    init_db(engine)
    return SqliteRepository(create_session_factory(engine))


def test_relay_persists_events_and_final_assistant_message(tmp_path: Path) -> None:
    db_path = tmp_path / "relay.sqlite3"
    repository = build_repository(db_path)
    agent = repository.create_agent(
        name="agent-1",
        runtime_type="local_process",
        adapter_type="http",
        workspace_path="/tmp/agent-1",
        idle_timeout_seconds=300,
    )
    session = repository.create_session(agent.id)

    result = relay_and_persist(
        agent_id=agent.id,
        session_id=session.id,
        event_iter=iter(
            [
                {"type": "delta", "delta": "hel"},
                {"type": "delta", "delta": "lo"},
                {"type": "done"},
                {"type": "delta", "delta": "!ignored!"},
            ]
        ),
        repository=repository,
    )

    assert result.events == [
        {"type": "delta", "delta": "hel"},
        {"type": "delta", "delta": "lo"},
        {"type": "done"},
    ]
    assert result.output == "hello"
    assert result.assistant_message_id is not None

    engine = create_sqlite_engine(f"sqlite:///{db_path}")
    session_factory = create_session_factory(engine)
    with session_factory() as db:
        stored_events = db.query(MessageEventORM).order_by(MessageEventORM.seq_no.asc()).all()
        stored_messages = db.query(MessageORM).order_by(MessageORM.created_at.asc()).all()

    assert [event.seq_no for event in stored_events] == [1, 2, 3]
    assert [event.event_type for event in stored_events] == ["delta", "delta", "done"]
    assert [event.payload_json for event in stored_events] == result.events
    assert {event.message_id for event in stored_events} == {result.assistant_message_id}
    assert len(stored_messages) == 1
    assert stored_messages[0].id == result.assistant_message_id
    assert stored_messages[0].role == "assistant"
    assert stored_messages[0].content == "hello"


def test_relay_stops_after_error_without_creating_assistant_message(tmp_path: Path) -> None:
    db_path = tmp_path / "relay.sqlite3"
    repository = build_repository(db_path)
    agent = repository.create_agent(
        name="agent-1",
        runtime_type="local_process",
        adapter_type="http",
        workspace_path="/tmp/agent-1",
        idle_timeout_seconds=300,
    )
    session = repository.create_session(agent.id)

    result = relay_and_persist(
        agent_id=agent.id,
        session_id=session.id,
        event_iter=iter(
            [
                {"type": "delta", "delta": "par"},
                {"type": "error", "message": "adapter failed", "code": "UPSTREAM_ERROR"},
                {"type": "done"},
            ]
        ),
        repository=repository,
    )

    assert result.events == [
        {"type": "delta", "delta": "par"},
        {"type": "error", "message": "adapter failed", "code": "UPSTREAM_ERROR"},
    ]
    assert result.output == "par"
    assert result.assistant_message_id is None

    engine = create_sqlite_engine(f"sqlite:///{db_path}")
    session_factory = create_session_factory(engine)
    with session_factory() as db:
        stored_events = db.query(MessageEventORM).order_by(MessageEventORM.seq_no.asc()).all()
        stored_messages = db.query(MessageORM).all()

    assert [event.seq_no for event in stored_events] == [1, 2]
    assert [event.event_type for event in stored_events] == ["delta", "error"]
    assert [event.payload_json for event in stored_events] == result.events
    assert {event.message_id for event in stored_events} == {None}
    assert stored_messages == []


def test_repository_retries_message_event_seq_after_unique_conflict(tmp_path: Path) -> None:
    db_path = tmp_path / "relay.sqlite3"
    repository = build_repository(db_path)
    agent = repository.create_agent(
        name="agent-1",
        runtime_type="local_process",
        adapter_type="http",
        workspace_path="/tmp/agent-1",
        idle_timeout_seconds=300,
    )
    session = repository.create_session(agent.id)

    first_event_id, first_seq_no = repository.create_message_event_with_retry(
        agent_id=agent.id,
        session_id=session.id,
        event_type="delta",
        payload_json={"type": "delta", "delta": "a"},
        seq_no=1,
    )
    second_event_id, second_seq_no = repository.create_message_event_with_retry(
        agent_id=agent.id,
        session_id=session.id,
        event_type="delta",
        payload_json={"type": "delta", "delta": "b"},
        seq_no=1,
    )

    assert first_event_id != second_event_id
    assert first_seq_no == 1
    assert second_seq_no == 2

    engine = create_sqlite_engine(f"sqlite:///{db_path}")
    session_factory = create_session_factory(engine)
    with session_factory() as db:
        stored_events = db.query(MessageEventORM).order_by(MessageEventORM.seq_no.asc()).all()

    assert [event.seq_no for event in stored_events] == [1, 2]
    assert [event.payload_json for event in stored_events] == [
        {"type": "delta", "delta": "a"},
        {"type": "delta", "delta": "b"},
    ]
