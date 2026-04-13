from datetime import datetime

import pytest
from pydantic import ValidationError

from src.api.schemas import (
    PaginationInfo,
    SessionEventItem,
    SessionEventPage,
    SessionResponse,
)


class TestSessionResponse:
    def test_session_response_with_new_fields(self):
        now = datetime.utcnow()
        session = SessionResponse(
            id="session-123",
            agent_id="agent-456",
            status="running",
            context_initialized=True,
            runtime_type="websocket",
            created_at=now,
            updated_at=now,
        )
        assert session.id == "session-123"
        assert session.agent_id == "agent-456"
        assert session.status == "running"
        assert session.context_initialized is True
        assert session.runtime_type == "websocket"
        assert session.created_at == now
        assert session.updated_at == now

    def test_session_response_defaults(self):
        now = datetime.utcnow()
        session = SessionResponse(
            id="session-123",
            agent_id="agent-456",
            status="running",
            created_at=now,
            updated_at=now,
        )
        assert session.context_initialized is False
        assert session.runtime_type is None


class TestSessionEventItem:
    def test_session_event_item(self):
        now = datetime.utcnow()
        item = SessionEventItem(
            id="event-1",
            session_id="session-123",
            type="message",
            source="agent",
            payload={"content": "hello"},
            timestamp=now,
        )
        assert item.id == "event-1"
        assert item.session_id == "session-123"
        assert item.type == "message"
        assert item.source == "agent"
        assert item.payload == {"content": "hello"}
        assert item.timestamp == now

    def test_session_event_item_optional_source(self):
        now = datetime.utcnow()
        item = SessionEventItem(
            id="event-1",
            session_id="session-123",
            type="message",
            payload={"content": "hello"},
            timestamp=now,
        )
        assert item.source is None


class TestPaginationInfo:
    def test_pagination_info(self):
        pagination = PaginationInfo(offset=0, limit=10, total=100)
        assert pagination.offset == 0
        assert pagination.limit == 10
        assert pagination.total == 100


class TestSessionEventPage:
    def test_session_event_page(self):
        now = datetime.utcnow()
        item = SessionEventItem(
            id="event-1",
            session_id="session-123",
            type="message",
            payload={"content": "hello"},
            timestamp=now,
        )
        pagination = PaginationInfo(offset=0, limit=10, total=1)
        page = SessionEventPage(items=[item], pagination=pagination)
        assert len(page.items) == 1
        assert page.pagination.total == 1
