from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from witty_service.api.services import build_default_services
from witty_service.config import get_settings
from witty_service.domain.enums import AgentKind, SessionOrigin
from witty_service.main import create_app
from witty_service.runtime.manager import RuntimeStatus, WorkspaceAvailability


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


def _workspace(container) -> str:
    workspace = container.workspaces.get_default()
    assert workspace is not None
    return workspace.id


def test_create_session_inherits_workspace_defaults(api) -> None:
    client, container = api
    workspace_id = _workspace(container)

    response = client.post(
        "/api/sessions",
        json={"workspace_id": workspace_id, "title": "First"},
        headers=_auth(),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["workspace_id"] == workspace_id
    assert body["title"] == "First"
    assert body["pinned"] is False
    assert body["origin"] == "web"
    assert body["current_agent"]["id"] == container.agents.get_primary().id
    assert body["current_model"] is None
    assert body["active_turn"] is None
    assert body["runtime_status"] == "stopped"
    assert container.sessions.get(body["id"]).runtime_session_id is None


def test_session_runtime_status_uses_manager_availability(api, monkeypatch) -> None:
    client, container = api
    workspace_id = _workspace(container)
    created = client.post(
        "/api/sessions",
        json={"workspace_id": workspace_id, "title": "First"},
        headers=_auth(),
    )
    session_id = created.json()["id"]
    monkeypatch.setattr(
        container.runtime_manager,
        "availability",
        lambda workspace_id: WorkspaceAvailability(
            workspace_id=workspace_id,
            status=RuntimeStatus.RESTARTING,
            detail="workspace runtime is restarting",
        ),
    )

    response = client.get(f"/api/sessions/{session_id}", headers=_auth())

    assert response.status_code == 200
    assert response.json()["runtime_status"] == "restarting"


def test_create_session_uses_explicit_agent_and_model(api) -> None:
    client, container = api
    workspace_id = _workspace(container)
    model = container.catalog_repository.create_model(
        "Model", "openai", "gpt-5", None, None, None, 1024, 0.2, 1_000
    )

    response = client.post(
        "/api/sessions",
        json={"workspace_id": workspace_id, "model_id": model.id},
        headers=_auth(),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["current_model"]["id"] == model.id
    assert body["current_model"]["enabled"] is True


def test_create_session_rejects_unknown_workspace_and_agent(api) -> None:
    client, container = api

    missing_workspace = client.post(
        "/api/sessions", json={"workspace_id": "ws_missing"}, headers=_auth()
    )
    assert missing_workspace.status_code == 404
    assert missing_workspace.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"

    missing_agent = client.post(
        "/api/sessions",
        json={"workspace_id": _workspace(container), "agent_id": "agt_missing"},
        headers=_auth(),
    )
    assert missing_agent.status_code == 404
    assert missing_agent.json()["error"]["code"] == "AGENT_NOT_FOUND"


def test_create_session_rejects_archived_agent(api) -> None:
    client, container = api
    expert = container.agents.create("Expert", AgentKind.expert, 1_000)
    container.agents.archive(expert.id, 2_000)

    response = client.post(
        "/api/sessions",
        json={"workspace_id": _workspace(container), "agent_id": expert.id},
        headers=_auth(),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AGENT_ARCHIVED"


def test_create_session_prefers_missing_model_over_archived_agent(api) -> None:
    client, container = api
    expert = container.agents.create("Expert", AgentKind.expert, 1_000)
    container.agents.archive(expert.id, 2_000)

    response = client.post(
        "/api/sessions",
        json={
            "workspace_id": _workspace(container),
            "agent_id": expert.id,
            "model_id": "mdl_missing",
        },
        headers=_auth(),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MODEL_NOT_FOUND"


def test_list_sessions_is_pinned_then_recent_with_cursor(api) -> None:
    client, container = api
    workspace_id = _workspace(container)
    primary = container.agents.get_primary()
    assert primary is not None

    first = container.sessions.create(
        workspace_id, primary.id, None, SessionOrigin.web, "first", None, None, 1_000
    )
    second = container.sessions.create(
        workspace_id, primary.id, None, SessionOrigin.web, "second", None, None, 2_000
    )
    third = container.sessions.create(
        workspace_id, primary.id, None, SessionOrigin.web, "third", None, None, 3_000
    )
    container.sessions.update_metadata(first.id, None, 1, 4_000)

    page_one = client.get(
        f"/api/workspaces/{workspace_id}/sessions",
        params={"limit": 2},
        headers=_auth(),
    )
    assert page_one.status_code == 200
    body = page_one.json()
    assert [item["id"] for item in body["items"]] == [first.id, third.id]
    assert body["next_cursor"] is not None

    page_two = client.get(
        f"/api/workspaces/{workspace_id}/sessions",
        params={"limit": 2, "cursor": body["next_cursor"]},
        headers=_auth(),
    )
    assert page_two.status_code == 200
    assert [item["id"] for item in page_two.json()["items"]] == [second.id]
    assert page_two.json()["next_cursor"] is None


def test_list_sessions_rejects_invalid_cursor(api) -> None:
    client, container = api
    response = client.get(
        f"/api/workspaces/{_workspace(container)}/sessions",
        params={"cursor": "not-a-cursor"},
        headers=_auth(),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_CURSOR"

    missing_workspace = client.get(
        "/api/workspaces/ws_missing/sessions",
        params={"cursor": "not-a-cursor"},
        headers=_auth(),
    )
    assert missing_workspace.status_code == 400
    assert missing_workspace.json()["error"]["code"] == "INVALID_CURSOR"


def test_get_and_update_session(api) -> None:
    client, container = api
    workspace_id = _workspace(container)
    created = client.post(
        "/api/sessions", json={"workspace_id": workspace_id}, headers=_auth()
    ).json()

    patched = client.patch(
        f"/api/sessions/{created['id']}",
        json={"title": "Renamed", "pinned": True},
        headers=_auth(),
    )
    assert patched.status_code == 200
    assert patched.json()["title"] == "Renamed"
    assert patched.json()["pinned"] is True

    fetched = client.get(f"/api/sessions/{created['id']}", headers=_auth())
    assert fetched.status_code == 200
    assert fetched.json()["title"] == "Renamed"

    missing = client.get("/api/sessions/ses_missing", headers=_auth())
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "SESSION_NOT_FOUND"
