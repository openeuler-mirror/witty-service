from __future__ import annotations

from typing import Any

import pytest

from witty_service.application.artifact_paths import normalize_artifact_event

WORKSPACE = "/root/.witty/agent-workspaces/b07433d1-0416-4a48-b24b-a1123f7abe0a/workspace"


def _artifact_event(
    relative_path: str, *, content: str | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": relative_path,
        "name": relative_path.rsplit("/", 1)[-1],
        "type": "code",
        "status": "ready",
        "relative_path": relative_path,
        "size": 1,
        "mime": "text/x-python",
    }
    if content is not None:
        payload["content"] = content
    return {"type": "artifact.completed", "payload": payload}


def test_normalizes_absolute_path_inside_workspace() -> None:
    raw = f"{WORKSPACE}/output/app.py"
    event = _artifact_event(raw, content="print('hi')")

    result = normalize_artifact_event(event, workspace_path=WORKSPACE)

    assert result is not None
    assert result["payload"]["relative_path"] == "output/app.py"
    assert result["payload"]["id"] == "output/app.py"
    assert result["payload"]["content"] == "print('hi')"
    assert result["type"] == "artifact.completed"


def test_drops_event_with_path_outside_workspace() -> None:
    assert normalize_artifact_event(
        _artifact_event("/etc/passwd"), workspace_path=WORKSPACE
    ) is None
    assert normalize_artifact_event(
        _artifact_event("/tmp/evil.py", content="x"), workspace_path=WORKSPACE
    ) is None


def test_normalizes_relative_path_inside_workspace() -> None:
    event = _artifact_event("output/app.py")
    result = normalize_artifact_event(event, workspace_path=WORKSPACE)
    assert result is not None
    assert result["payload"]["relative_path"] == "output/app.py"
    assert result["payload"]["id"] == "output/app.py"


def test_drops_relative_path_escaping_workspace() -> None:
    assert (
        normalize_artifact_event(
            _artifact_event("../../etc/passwd"), workspace_path=WORKSPACE
        )
        is None
    )
    assert (
        normalize_artifact_event(
            _artifact_event("output/../../secret.txt"), workspace_path=WORKSPACE
        )
        is None
    )


def test_passes_through_non_artifact_events() -> None:
    event = {"type": "tool.call.started", "payload": {}}
    assert normalize_artifact_event(event, workspace_path=WORKSPACE) is event


def test_drops_missing_payload_or_path() -> None:
    # 缺失/空/非字符串 relative_path 无法做包含校验，必须丢弃而不是透传。
    assert (
        normalize_artifact_event(
            {"type": "artifact.completed", "payload": {}},
            workspace_path=WORKSPACE,
        )
        is None
    )
    assert (
        normalize_artifact_event(
            {"type": "artifact.completed", "payload": {"relative_path": ""}},
            workspace_path=WORKSPACE,
        )
        is None
    )
    assert (
        normalize_artifact_event(
            {"type": "artifact.delta", "payload": {"relative_path": None}},
            workspace_path=WORKSPACE,
        )
        is None
    )
    assert (
        normalize_artifact_event(
            {"type": "artifact.completed", "payload": None},
            workspace_path=WORKSPACE,
        )
        is None
    )


def test_strips_content_for_sensitive_files() -> None:
    event = _artifact_event("config/credentials.json", content="secret")
    result = normalize_artifact_event(event, workspace_path=WORKSPACE)
    assert result is not None
    assert result["payload"]["relative_path"] == "config/credentials.json"
    assert "content" not in result["payload"]

    non_sensitive = _artifact_event("output/app.py", content="print('ok')")
    result = normalize_artifact_event(non_sensitive, workspace_path=WORKSPACE)
    assert result is not None
    assert result["payload"]["content"] == "print('ok')"


@pytest.mark.parametrize(
    "event_type",
    ["artifact.started", "artifact.delta", "artifact.completed"],
)
def test_normalizes_all_artifact_event_types(event_type: str) -> None:
    raw = f"{WORKSPACE}/notes.md"
    result = normalize_artifact_event(
        {"type": event_type, "payload": {"id": raw, "relative_path": raw}},
        workspace_path=WORKSPACE,
    )
    assert result is not None
    assert result["payload"]["relative_path"] == "notes.md"
