from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from witty_service.api.services import build_default_services
from witty_service.config import get_settings
from witty_service.domain.error_codes import ErrorCode
from witty_service.domain.errors import DomainError
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


def _create(client: TestClient, **overrides):
    payload = {
        "display_name": "Model",
        "provider_id": "openai",
        "provider_model_id": "gpt-5",
        "api_key": "sk-secret",
        **overrides,
    }
    return client.post("/api/models", json=payload, headers=_auth())


def test_create_requires_key_for_cloud_provider(api) -> None:
    client, _ = api

    response = _create(client, api_key=None)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "MODEL_CREDENTIAL_REQUIRED"

    local = _create(
        client, provider_id="ollama", provider_model_id="llama", api_key=None
    )
    assert local.status_code == 201
    assert local.json()["has_credential"] is False
    assert local.json()["secret_revision"] == 0

    custom = _create(client, provider_id="custom", api_key=None)
    assert custom.status_code == 400
    assert custom.json()["error"]["code"] == "VALIDATION_ERROR"


def test_same_provider_reuses_credential_and_rejects_conflicts(api) -> None:
    client, _ = api
    first = _create(client)
    assert first.status_code == 201
    assert first.json()["has_credential"] is True
    assert first.json()["secret_revision"] == 1

    second = _create(
        client,
        display_name="Second",
        provider_model_id="gpt-5-mini",
        api_key=None,
    )
    assert second.status_code == 201
    assert second.json()["has_credential"] is True
    assert second.json()["secret_revision"] == 1

    credential_conflict = _create(client, provider_model_id="gpt-5-nano")
    assert credential_conflict.status_code == 409
    assert credential_conflict.json()["error"]["code"] == "MODEL_CREDENTIAL_CONFLICT"

    provider_conflict = _create(
        client,
        provider_model_id="gpt-5-large",
        api_base_url="https://example.com/v1",
    )
    assert provider_conflict.status_code == 409
    assert provider_conflict.json()["error"]["code"] == "MODEL_PROVIDER_CONFLICT"


def test_list_filters_disabled_models(api) -> None:
    client, _ = api
    created = _create(client).json()

    response = client.put(
        f"/api/models/{created['id']}",
        json={
            "display_name": "Model",
            "max_tokens": 4096,
            "temperature": 0.2,
            "enabled": False,
            "is_default": False,
        },
        headers=_auth(),
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is False

    assert client.get("/api/models", headers=_auth()).json()["items"] == []
    assert (
        len(
            client.get("/api/models?include_disabled=true", headers=_auth()).json()[
                "items"
            ]
        )
        == 1
    )


def test_update_keeps_single_default_and_rejects_default_when_disabled(api) -> None:
    client, _ = api
    first = _create(client, is_default=True).json()
    second = _create(
        client,
        provider_id="anthropic",
        provider_model_id="claude-sonnet",
        compatibility="anthropic",
        api_key="sk-ant",
    ).json()

    update = {
        "display_name": "Renamed",
        "max_tokens": 8192,
        "temperature": 1.2,
        "enabled": True,
        "is_default": True,
    }
    assert (
        client.put(
            f"/api/models/{second['id']}", json=update, headers=_auth()
        ).status_code
        == 200
    )
    models = {
        item["id"]: item
        for item in client.get("/api/models", headers=_auth()).json()["items"]
    }
    assert models[second["id"]]["is_default"] is True
    assert models[first["id"]]["is_default"] is False
    assert models[second["id"]]["display_name"] == "Renamed"
    assert models[second["id"]]["max_tokens"] == 8192
    assert models[second["id"]]["temperature"] == 1.2

    disabled_default = {**update, "enabled": False, "is_default": True}
    response = client.put(
        f"/api/models/{second['id']}", json=disabled_default, headers=_auth()
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MODEL_DISABLED"

    disabled = {**update, "enabled": False, "is_default": False}
    response = client.put(f"/api/models/{second['id']}", json=disabled, headers=_auth())
    assert response.status_code == 200
    assert response.json()["is_default"] is False

    with_key = {**update, "api_key": "sk-not-allowed"}
    rejected = client.put(f"/api/models/{second['id']}", json=with_key, headers=_auth())
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"


def test_rotate_updates_whole_provider_group(api) -> None:
    client, container = api
    first = _create(client).json()
    second = _create(
        client,
        display_name="Second",
        provider_model_id="gpt-5-mini",
        api_key=None,
    ).json()

    response = client.put(
        f"/api/models/{first['id']}/credential",
        json={"api_key": "sk-rotated"},
        headers=_auth(),
    )

    assert response.status_code == 200
    assert response.json()["secret_revision"] == 2
    models = {
        item["id"]: item
        for item in client.get("/api/models", headers=_auth()).json()["items"]
    }
    assert models[first["id"]]["secret_revision"] == 2
    assert models[second["id"]]["secret_revision"] == 2
    assert (
        len(list(Path(container.model_catalog._credentials.directory).glob("*.json")))
        == 1
    )


def test_delete_is_blocked_by_references_and_removes_orphan_credential(api) -> None:
    client, container = api
    model = _create(client).json()

    with container.engine.begin() as connection:
        connection.execute(
            text("UPDATE workspaces SET default_model_id = :model_id"),
            {"model_id": model["id"]},
        )

    blocked = client.delete(f"/api/models/{model['id']}", headers=_auth())
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "MODEL_IN_USE"
    assert blocked.json()["error"]["details"]["references"]["workspaces"] == 1

    with container.engine.begin() as connection:
        connection.execute(text("UPDATE workspaces SET default_model_id = NULL"))
    deleted = client.delete(f"/api/models/{model['id']}", headers=_auth())
    assert deleted.status_code == 204
    assert client.get("/api/models", headers=_auth()).json()["items"] == []
    assert (
        list(Path(container.model_catalog._credentials.directory).glob("*.json")) == []
    )


def test_credential_store_failure_returns_503(
    api, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, container = api

    def unavailable(*args, **kwargs):
        raise DomainError(
            code=ErrorCode.MODEL_CREDENTIAL_UNAVAILABLE,
            message="Model credential store is not usable.",
            status_code=503,
        )

    monkeypatch.setattr(container.model_catalog._credentials, "write", unavailable)

    response = _create(client)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MODEL_CREDENTIAL_UNAVAILABLE"
    assert "sk-secret" not in response.text


def test_missing_model_returns_404(api) -> None:
    client, _ = api
    update = {
        "display_name": "Model",
        "max_tokens": 4096,
        "temperature": 0.2,
        "enabled": True,
        "is_default": False,
    }

    assert (
        client.put("/api/models/mdl_missing", json=update, headers=_auth()).status_code
        == 404
    )
    assert (
        client.put(
            "/api/models/mdl_missing/credential",
            json={"api_key": "sk-new"},
            headers=_auth(),
        ).status_code
        == 404
    )
    assert client.delete("/api/models/mdl_missing", headers=_auth()).status_code == 404
