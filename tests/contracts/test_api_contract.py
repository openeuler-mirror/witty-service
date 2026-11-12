from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from openapi_spec_validator import validate

from witty_service.api.contract.openapi import (
    render_contract_openapi,
    render_contract_openapi_yaml,
)
from witty_service.api.errors import ERROR_PRIORITY, ERROR_STATUS
from witty_service.api.openapi import _finalize
from witty_service.api.services import build_default_services
from witty_service.config import get_settings
from witty_service.domain.error_codes import ErrorCode
from witty_service.main import create_app

DOCUMENT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "agent-session-workspace-openapi.yaml"
)


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    return render_contract_openapi()


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv(
        "WITTY_DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'db.sqlite3'}"
    )
    monkeypatch.setenv("WITTY_WORKSPACE_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv(
        "WITTY_MODEL_CREDENTIALS_DIR", str(tmp_path / "model-credentials")
    )
    monkeypatch.setattr("witty_service.config._settings", None)
    monkeypatch.setattr("witty_service.main.configure_logging", lambda: None)
    return TestClient(create_app(services=build_default_services()))


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().auth_token}"}


def test_draft_document_matches_generated_contract() -> None:
    assert DOCUMENT.read_text(encoding="utf-8") == render_contract_openapi_yaml(), (
        "OpenAPI Draft 已过期，运行 `python scripts/render_openapi.py` 重新生成"
    )


def test_generated_contract_is_valid_openapi(spec: dict[str, Any]) -> None:
    validate(spec)


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/workspaces", None),
        (
            "POST",
            "/api/workspaces",
            {"name": "demo", "host_directory": "/tmp/demo"},
        ),
        ("GET", "/api/workspaces/ws_01", None),
        ("PATCH", "/api/workspaces/ws_01", {"name": "renamed"}),
        ("DELETE", "/api/sessions/ses_01", None),
        ("POST", "/api/sessions/ses_01/turns/trn_01/abort", None),
        ("POST", "/api/sessions/ses_01/turns/trn_01/regenerate", None),
        ("GET", "/api/agents", None),
        ("GET", "/api/agents/agt_01", None),
        ("POST", "/api/agents", {"name": "demo"}),
        (
            "PUT",
            "/api/agents/agt_01",
            {
                "name": "demo",
                "description": "",
                "system_prompt": "",
                "expected_config_updated_at": "2026-11-09T07:30:00.000Z",
            },
        ),
        ("POST", "/api/agents/agt_01/archive", None),
    ],
)
def test_unimplemented_operations_return_501(
    client: TestClient,
    auth: dict[str, str],
    method: str,
    path: str,
    body: dict[str, Any] | None,
) -> None:
    response = client.request(method, path, json=body, headers=auth)

    assert response.status_code == 501
    assert response.json()["error"]["code"] == "ENDPOINT_NOT_IMPLEMENTED"


def test_validation_failure_returns_400_without_echoing_input(
    client: TestClient, auth: dict[str, str]
) -> None:
    canary = "INPUT-CANARY-4f8a1c"
    response = client.post(
        "/api/sessions", json={"workspace_id": {"value": canary}}, headers=auth
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    details = error["details"]["errors"]
    assert len(details) == 1
    assert details[0]["field"] == "workspace_id"
    assert details[0]["type"] == "string_type"
    assert details[0]["reason"]
    assert canary not in response.text


def test_patch_rejects_empty_update(client: TestClient, auth: dict[str, str]) -> None:
    response = client.patch("/api/sessions/ses_01/selection", json={}, headers=auth)

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["errors"][0]["field"] == ""
    assert error["details"]["errors"][0]["type"] == "value_error"


@pytest.mark.parametrize(
    ("path", "body", "field"),
    [
        ("/api/sessions/ses_01", {"title": None}, "title"),
        ("/api/sessions/ses_01", {"pinned": None}, "pinned"),
        ("/api/sessions/ses_01/selection", {"agent_id": None}, "agent_id"),
        (
            "/api/sessions/ses_01/selection",
            {"agent_id": "agt_01", "model_id": None},
            "model_id",
        ),
        (
            "/api/workspaces/ws_01",
            {"name": None},
            "name",
        ),
    ],
)
def test_patch_rejects_null_with_field(
    client: TestClient,
    auth: dict[str, str],
    path: str,
    body: dict[str, Any],
    field: str,
) -> None:
    response = client.patch(path, json=body, headers=auth)

    assert response.status_code == 400
    assert response.json()["error"]["details"]["errors"][0]["field"] == field


@pytest.mark.parametrize(
    ("schema_name", "nullable_fields"),
    [
        ("UpdateSessionRequest", set()),
        ("UpdateSessionSelectionRequest", set()),
        ("UpdateWorkspaceRequest", {"default_agent_id", "default_model_id"}),
    ],
)
def test_patch_schemas_validate_presence_and_nullability(
    spec: dict[str, Any], schema_name: str, nullable_fields: set[str]
) -> None:
    schema = spec["components"]["schemas"][schema_name]

    assert schema["minProperties"] == 1
    for name, prop in schema["properties"].items():
        assert "default" not in prop
        nullable = prop.get("type") == "null" or any(
            variant.get("type") == "null" for variant in prop.get("anyOf", [])
        )
        assert nullable is (name in nullable_fields)


@pytest.mark.parametrize("field", ["default_agent_id", "default_model_id"])
def test_workspace_patch_accepts_explicit_null_to_clear(
    client: TestClient, auth: dict[str, str], field: str
) -> None:
    response = client.patch("/api/workspaces/ws_01", json={field: None}, headers=auth)

    assert response.status_code == 501


def test_file_parts_must_stay_inside_the_workspace(
    client: TestClient, auth: dict[str, str]
) -> None:
    response = client.post(
        "/api/sessions/ses_01/turns",
        json={
            "parts": [
                {"type": "file", "relative_path": "../escape", "mime": "text/plain"}
            ]
        },
        headers=auth,
    )

    assert response.status_code == 400
    error = response.json()["error"]["details"]["errors"][0]
    assert error["field"] == "parts.0.file.relative_path"
    assert error["type"] == "value_error"
    assert error["reason"] == "path must not escape the workspace"


def test_workspace_paths_allow_colon_names_and_reject_empty(
    client: TestClient, auth: dict[str, str]
) -> None:
    accepted = client.post(
        "/api/sessions/ses_01/turns",
        json={
            "parts": [
                {"type": "file", "relative_path": "dir/a:b.txt", "mime": "text/plain"}
            ]
        },
        headers=auth,
    )
    empty = client.post(
        "/api/sessions/ses_01/turns",
        json={"parts": [{"type": "file", "relative_path": "", "mime": "text/plain"}]},
        headers=auth,
    )

    assert accepted.status_code == 404
    assert accepted.json()["error"]["code"] == "SESSION_NOT_FOUND"
    assert empty.status_code == 400
    error = empty.json()["error"]["details"]["errors"][0]
    assert error["field"] == "parts.0.file.relative_path"
    assert error["type"] == "value_error"
    assert error["reason"] == "path must not be empty"


def test_workspace_paths_reject_drive_relative_paths(
    client: TestClient, auth: dict[str, str]
) -> None:
    response = client.post(
        "/api/sessions/ses_01/turns",
        json={
            "parts": [{"type": "file", "relative_path": "C:foo", "mime": "text/plain"}]
        },
        headers=auth,
    )

    assert response.status_code == 400
    error = response.json()["error"]["details"]["errors"][0]
    assert error["field"] == "parts.0.file.relative_path"
    assert error["reason"] == "path must be relative to the workspace"


def test_missing_token_returns_401_envelope_with_challenge(
    client: TestClient,
) -> None:
    response = client.get("/api/workspaces")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_api_unknown_route_and_method_use_error_envelope(
    client: TestClient, auth: dict[str, str]
) -> None:
    missing = client.get("/api/nope", headers=auth)
    wrong_method = client.put("/api/workspaces", headers=auth)

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "ENDPOINT_NOT_FOUND"
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_unhandled_api_error_uses_error_envelope() -> None:
    from witty_service.api.errors import register_exception_handlers
    from witty_service.api.openapi import ApiApp

    app = ApiApp()
    register_exception_handlers(app)

    @app.get("/api/boom")
    def boom() -> None:
        raise RuntimeError("boom")

    response = TestClient(app, raise_server_exceptions=False).get("/api/boom")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"


def test_error_responses_use_envelope_and_exclude_422(spec: dict[str, Any]) -> None:
    assert ERROR_PRIORITY == (400, 404, 409, 503)
    assert {ERROR_STATUS[code] for code in ErrorCode} == {
        400,
        401,
        404,
        405,
        409,
        500,
        501,
        503,
    }

    for path_item in spec["paths"].values():
        for operation in path_item.values():
            for status, response in operation["responses"].items():
                assert status != "422"
                if int(status) < 400:
                    continue
                assert response["content"]["application/json"]["schema"] == {
                    "$ref": "#/components/schemas/ErrorEnvelope"
                }


def test_401_documents_bearer_challenge(spec: dict[str, Any]) -> None:
    response = spec["paths"]["/api/workspaces"]["get"]["responses"]["401"]

    assert "WWW-Authenticate" in response["headers"]


def test_abort_is_idempotent_in_contract(spec: dict[str, Any]) -> None:
    responses = spec["paths"]["/api/sessions/{session_id}/turns/{turn_id}/abort"][
        "post"
    ]["responses"]

    assert set(responses) == {"200", "401", "404", "503"}
    abort_schema = spec["components"]["schemas"]["AbortTurnResponse"]
    assert "already_stopped" in abort_schema["required"]
    assert abort_schema["examples"][0]["already_stopped"] is True
    assert abort_schema["examples"][0]["turn"]["status"] == "interrupted"


def test_contract_excludes_publish_and_removed_routes(spec: dict[str, Any]) -> None:
    assert all(path.startswith("/api/") for path in spec["paths"])
    assert not [path for path in spec["paths"] if "publish" in path]
    assert not [path for path in spec["paths"] if path.startswith("/api/turns/")]
    assert not [
        path
        for path in spec["paths"]
        if path.startswith("/api/agents/") and "session" in path
    ]
    assert "publish_id" not in json.dumps(spec)


def test_workspace_responses_hide_host_directory(spec: dict[str, Any]) -> None:
    schemas = spec["components"]["schemas"]
    assert "host_directory" in schemas["CreateWorkspaceRequest"]["properties"]
    assert "host_directory" not in json.dumps(schemas["WorkspaceResponse"])


def test_error_codes_keep_specific_not_found(spec: dict[str, Any]) -> None:
    codes = set(spec["components"]["schemas"]["ErrorCode"]["enum"])

    assert {
        "WORKSPACE_NOT_FOUND",
        "SESSION_NOT_FOUND",
        "TURN_NOT_FOUND",
        "AGENT_NOT_FOUND",
        "MODEL_NOT_FOUND",
    } <= codes
    assert not codes & {
        "RESOURCE_NOT_FOUND",
        "SESSION_BUSY",
        "TURN_NOT_RUNNING",
        "REGENERATE_TARGET_NOT_FOUND",
        "RUNTIME_LEASE_CONFLICT",
    }


def test_finalize_only_relaxes_api_paths() -> None:
    spec: dict[str, Any] = {
        "paths": {
            "/api/items": {
                "parameters": [{"name": "q", "in": "query"}],
                "get": {"responses": {"422": {}, "200": {}}},
            },
            "/legacy/items": {"get": {"responses": {"422": {}, "200": {}}}},
        }
    }

    _finalize(spec)

    assert "422" not in spec["paths"]["/api/items"]["get"]["responses"]
    assert "422" in spec["paths"]["/legacy/items"]["get"]["responses"]


def test_service_app_mounts_contract_without_422_docs(monkeypatch) -> None:
    from witty_service import main as main_module

    monkeypatch.setattr(main_module, "configure_logging", lambda: None)
    app = main_module.create_app(services=MagicMock())

    paths = app.openapi()["paths"]
    assert "/api/sessions/{session_id}/turns" in paths
    assert "422" not in paths["/api/workspaces"]["get"]["responses"]
    assert "/agents" not in paths
