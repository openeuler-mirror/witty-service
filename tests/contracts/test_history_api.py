from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from witty_service.api.services import build_default_services
from witty_service.application.history_service import (
    HistoryMessage,
    MessagePage,
    SessionEventView,
    SessionResyncView,
)
from witty_service.config import get_settings
from witty_service.domain.enums import MessageRole, SessionOrigin
from witty_service.main import create_app
from witty_service.runtime.contracts import (
    RuntimeHistoryMessage,
    RuntimeMessageKind,
    RuntimeTextPart,
)


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "WITTY_DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'db.sqlite3'}"
    )
    monkeypatch.setenv("WITTY_WORKSPACE_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv(
        "WITTY_MODEL_CREDENTIALS_DIR", str(tmp_path / "model-credentials")
    )
    monkeypatch.setattr("witty_service.config._settings", None)
    monkeypatch.setattr("witty_service.main.configure_logging", lambda: None)
    container = build_default_services()
    app = create_app(services=container)
    with TestClient(app) as client:
        yield client, container


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().auth_token}"}


def _session(container, *, runtime_session_id: str | None = None) -> str:
    workspace = container.workspaces.get_default()
    primary = container.agents.get_primary()
    assert workspace is not None and primary is not None
    model = container.catalog_repository.create_model(
        "model", "openai", "gpt-test", None, None, None, 1024, 0.2, 1_000
    )
    session = container.sessions.create(
        workspace.id,
        primary.id,
        model.id,
        SessionOrigin.web,
        "Session",
        None,
        None,
        1_000,
    )
    if runtime_session_id is not None:
        container.sessions.set_runtime_session_id(session.id, runtime_session_id, 1_001)
    return session.id


def test_list_messages_projects_items_and_cursors(api, monkeypatch) -> None:
    client, container = api
    session_id = _session(container, runtime_session_id="rt_1")

    async def list_messages(session, *, limit, cursor):
        return MessagePage(
            items=(
                HistoryMessage(
                    message=RuntimeHistoryMessage(
                        message_id="msg_1",
                        kind=RuntimeMessageKind.ASSISTANT,
                        created_at=1_100,
                        parts=(RuntimeTextPart(text="answer"),),
                    ),
                    turn=None,
                ),
            ),
            next_cursor="next_cursor",
            previous_cursor="previous_cursor",
        )

    monkeypatch.setattr(container.history_service, "list_messages", list_messages)

    response = client.get(f"/api/sessions/{session_id}/messages", headers=_auth())

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": "msg_1",
                "role": "assistant",
                "attribution": None,
                "parts": [{"type": "text", "text": "answer"}],
                "created_at": "1970-01-01T00:00:01.100Z",
            }
        ],
        "next_cursor": "next_cursor",
        "previous_cursor": "previous_cursor",
    }


def test_list_messages_rejects_invalid_cursor(api) -> None:
    client, container = api
    session_id = _session(container)

    response = client.get(
        f"/api/sessions/{session_id}/messages",
        params={"cursor": "broken"},
        headers=_auth(),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CURSOR"


def test_list_messages_returns_503_when_runtime_unavailable(api) -> None:
    client, container = api
    session_id = _session(container, runtime_session_id="rt_1")

    response = client.get(f"/api/sessions/{session_id}/messages", headers=_auth())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RUNTIME_UNAVAILABLE"


def test_list_messages_has_no_db_fallback_when_runtime_unavailable(api) -> None:
    client, container = api
    session_id = _session(container, runtime_session_id="rt_1")
    session = container.sessions.get(session_id)
    assert session is not None
    turn = container.sessions.start_turn(
        session_id, session.current_agent_id, session.current_model_id, 1_100
    )
    container.sessions.add_runtime_message(
        "msg_user", turn.id, MessageRole.user, 0, 1_100
    )

    response = client.get(f"/api/sessions/{session_id}/messages", headers=_auth())

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RUNTIME_UNAVAILABLE"


def test_stream_events_formats_sse_frames(api, monkeypatch) -> None:
    client, container = api
    session_id = _session(container)

    async def stream_events(session):
        async def frames():
            yield SessionEventView(
                event_id="evt_1",
                type="message.delta",
                session_id=session,
                turn_id="trn_1",
                message_id="msg_1",
                occurred_at=1_100,
                payload={"delta": "chunk"},
            )
            yield SessionResyncView(reason="subscriber_lagged", last_durable_seq=7)

        return frames()

    monkeypatch.setattr(container.history_service, "stream_events", stream_events)

    response = client.get(f"/api/sessions/{session_id}/events", headers=_auth())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert frames[0]["type"] == "message.delta"
    assert frames[0]["payload"] == {"delta": "chunk"}
    assert frames[1]["type"] == "resync_required"
    assert frames[1]["payload"] == {
        "reason": "subscriber_lagged",
        "last_durable_seq": 7,
        "full_reload": True,
    }


def test_stream_events_returns_404_for_unknown_session(api) -> None:
    client, _container = api

    response = client.get("/api/sessions/ses_missing/events", headers=_auth())

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "SESSION_NOT_FOUND"
