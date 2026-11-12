from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from witty_service.api.services import build_default_services
from witty_service.config import get_settings
from witty_service.domain.enums import SessionOrigin, TurnStatus
from witty_service.main import create_app


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


def _session(container, *, model_id: str | None) -> str:
    workspace = container.workspaces.get_default()
    primary = container.agents.get_primary()
    assert workspace is not None and primary is not None
    return container.sessions.create(
        workspace.id, primary.id, model_id, SessionOrigin.web, "Session", None, None, 1_000
    ).id


def _model(container, name: str):
    return container.catalog_repository.create_model(
        name, "openai", f"model-{name}", None, None, None, 1024, 0.2, 1_000
    )


def test_turn_attribution_uses_admission_snapshots(api) -> None:
    client, container = api
    first_model = _model(container, "first")
    second_model = _model(container, "second")
    session_id = _session(container, model_id=first_model.id)

    turn = container.turn_service.start_turn(session_id)
    container.sessions.finish_turn(turn.id, TurnStatus.completed, None, None, 1_100)
    primary = container.agents.get_primary()
    assert primary is not None
    container.agents.update_configuration(
        primary.id,
        primary.config_updated_at,
        "Renamed Primary",
        None,
        None,
        1_200,
    )
    container.catalog_repository.update_model(
        first_model.id,
        display_name="Renamed Model",
        max_tokens=1024,
        temperature=0.2,
        enabled=1,
        is_default=0,
        now=1_200,
    )
    client.patch(
        f"/api/sessions/{session_id}/selection",
        json={"model_id": second_model.id},
        headers=_auth(),
    ).raise_for_status()

    response = client.get(f"/api/sessions/{session_id}/turns", headers=_auth())

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["agent"] == {"id": primary.id, "name": "Primary"}
    assert item["model"] == {"id": first_model.id, "display_name": "first"}
    assert item["usage"] is None


def test_list_turns_paginates_descending(api) -> None:
    client, container = api
    model = _model(container, "model")
    session_id = _session(container, model_id=model.id)
    created = []
    for index in range(3):
        turn = container.turn_service.start_turn(session_id)
        container.sessions.finish_turn(
            turn.id, TurnStatus.completed, None, None, 1_100 + index
        )
        created.append(turn.id)

    first = client.get(
        f"/api/sessions/{session_id}/turns",
        params={"limit": 2},
        headers=_auth(),
    )
    assert first.status_code == 200
    assert [item["id"] for item in first.json()["items"]] == [
        created[2],
        created[1],
    ]
    cursor = first.json()["next_cursor"]
    assert cursor is not None

    second = client.get(
        f"/api/sessions/{session_id}/turns",
        params={"limit": 2, "cursor": cursor},
        headers=_auth(),
    )
    assert second.status_code == 200
    assert [item["id"] for item in second.json()["items"]] == [created[0]]
    assert second.json()["next_cursor"] is None


def test_list_turns_rejects_cross_session_and_invalid_cursor(api) -> None:
    client, container = api
    model = _model(container, "model")
    session_id = _session(container, model_id=model.id)
    other_id = _session(container, model_id=model.id)
    turn = container.turn_service.start_turn(session_id)
    container.sessions.finish_turn(turn.id, TurnStatus.completed, None, None, 1_100)
    other_turn = container.turn_service.start_turn(other_id)
    container.sessions.finish_turn(
        other_turn.id, TurnStatus.completed, None, None, 1_200
    )
    newer_other = container.turn_service.start_turn(other_id)
    container.sessions.finish_turn(
        newer_other.id, TurnStatus.completed, None, None, 1_300
    )
    other_cursor = client.get(
        f"/api/sessions/{other_id}/turns", params={"limit": 1}, headers=_auth()
    ).json()["next_cursor"]
    assert other_cursor is not None

    cross_session = client.get(
        f"/api/sessions/{session_id}/turns",
        params={"cursor": other_cursor},
        headers=_auth(),
    )
    assert cross_session.status_code == 400
    assert cross_session.json()["error"]["code"] == "INVALID_CURSOR"

    invalid = client.get(
        f"/api/sessions/{session_id}/turns",
        params={"cursor": "broken"},
        headers=_auth(),
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_CURSOR"

    negative_cursor = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"v": 1, "scope": "turn", "id": session_id, "turn_seq": -1}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    negative = client.get(
        f"/api/sessions/{session_id}/turns",
        params={"cursor": negative_cursor},
        headers=_auth(),
    )
    assert negative.status_code == 400
    assert negative.json()["error"]["code"] == "INVALID_CURSOR"

    missing = client.get("/api/sessions/ses_missing/turns", headers=_auth())
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "SESSION_NOT_FOUND"
