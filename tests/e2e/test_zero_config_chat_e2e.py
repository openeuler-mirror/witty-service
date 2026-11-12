"""T11 零配置直接对话与模型切换 E2E。

走真实生产组合根（SQLite + 真实 OpenCode 1.17.13 serve + 本地 OpenAI 兼容
provider）。主业务链路全部走 HTTP API，测试内部直接读取 container 做运行态
断言与停机模拟：

1. 默认 Workspace/Primary 下只创建模型、选择模型即可对话，不创建 Expert；
2. 同一 Session 切模型后下一 Turn 生效，旧 Turn 的归因快照不变；
3. 更新模型凭据（保存）不停服，下一次 Turn 触发发布；
4. Runtime 停止后历史读取返回 503，不回退 DB 正文。

provider 是 `tests.probes.mock_provider`，不消耗真实 LLM 凭据；真实模型只需把
创建模型时的 `api_base_url/api_key` 换成真实值即可复用同一链路。
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.probes.mock_provider import MockProvider
from tests.probes.probe_env import PROBE_OPENCODE_VERSION
from witty_service.api.services import build_default_services
from witty_service.config import get_settings
from witty_service.domain.enums import AgentKind
from witty_service.main import create_app
from witty_service.runtime.manager import RuntimePhase
from witty_service.runtime.publication import PublicationPhase


def _supported_opencode() -> str | None:
    binary = shutil.which("opencode")
    if binary is None:
        return None
    try:
        version = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10
        )
    except OSError:
        return None
    if version.returncode != 0:
        return None
    return binary if version.stdout.strip() == PROBE_OPENCODE_VERSION else None


pytestmark = pytest.mark.skipif(
    _supported_opencode() is None,
    reason=f"opencode {PROBE_OPENCODE_VERSION} is required for v2 chat e2e",
)

_TURN_TERMINAL = {"completed", "error", "interrupted"}


@pytest.fixture()
def live_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    (tmp_path / "tmp").mkdir()
    monkeypatch.setenv(
        "WITTY_DATABASE_URL", f"sqlite:///{tmp_path / 'data' / 'db.sqlite3'}"
    )
    monkeypatch.setenv("WITTY_WORKSPACE_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv(
        "WITTY_MODEL_CREDENTIALS_DIR", str(tmp_path / "model-credentials")
    )
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("TMP", str(tmp_path / "tmp"))
    monkeypatch.setenv("TEMP", str(tmp_path / "tmp"))
    monkeypatch.setattr("witty_service.config._settings", None)
    monkeypatch.setattr("witty_service.main.configure_logging", lambda: None)
    provider = MockProvider()
    try:
        provider.start()
        container = build_default_services()
        app = create_app(services=container)
        with TestClient(app) as client:
            yield client, container, provider, tmp_path
    finally:
        provider.stop()


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().auth_token}"}


def _create_model(
    client: TestClient,
    provider: MockProvider,
    *,
    name: str,
    model: str,
    api_key: str | None = None,
) -> dict[str, Any]:
    payload = {
        "display_name": name,
        "provider_id": "mock",
        "provider_model_id": model,
        "compatibility": "openai",
        "api_base_url": provider.base_url,
    }
    if api_key is not None:
        payload["api_key"] = api_key
    response = client.post("/api/models", json=payload, headers=_auth())
    assert response.status_code == 201, response.text
    return response.json()


def _wait_turn(
    client: TestClient, session_id: str, turn_id: str, *, timeout: float = 120.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/sessions/{session_id}/turns", headers=_auth())
        assert response.status_code == 200, response.text
        for turn in response.json()["items"]:
            if turn["id"] == turn_id and turn["status"] in _TURN_TERMINAL:
                return turn
        time.sleep(0.1)
    raise AssertionError(f"turn {turn_id} did not reach a terminal state")


def _wait_message_count(
    client: TestClient, session_id: str, count: int, *, timeout: float = 30.0
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/sessions/{session_id}/messages", headers=_auth())
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        if len(items) >= count:
            return items
        time.sleep(0.1)
    raise AssertionError(f"session {session_id} did not project {count} messages")


def _message_text(message: dict[str, Any]) -> str:
    return "".join(
        part.get("text", "") for part in message["parts"] if part.get("type") == "text"
    )


def test_zero_config_chat_and_model_switch(live_api) -> None:
    client, container, provider, tmp_path = live_api
    workspace = container.workspaces.get_default()
    primary = container.agents.get_primary()
    assert workspace is not None and primary is not None
    assert {agent.kind for agent in container.agents.list_catalog(False)} == {
        AgentKind.primary
    }

    model_one = _create_model(
        client, provider, name="Mock One", model="mock-model", api_key="probe-key"
    )
    model_two = _create_model(client, provider, name="Mock Two", model="mock-model-b")

    created = client.post(
        "/api/sessions",
        json={"workspace_id": workspace.id, "model_id": model_one["id"]},
        headers=_auth(),
    )
    assert created.status_code == 201, created.text
    session = created.json()
    assert session["current_agent"]["id"] == primary.id
    assert session["current_agent"]["kind"] == "primary"
    assert session["current_model"]["id"] == model_one["id"]
    session_id = session["id"]

    first = client.post(
        f"/api/sessions/{session_id}/turns",
        json={"parts": [{"type": "text", "text": "hello first turn"}]},
        headers=_auth(),
    )
    assert first.status_code == 201, first.text
    first_turn = first.json()
    assert first_turn["agent"]["id"] == primary.id
    assert first_turn["model"]["id"] == model_one["id"]
    assert _wait_turn(client, session_id, first_turn["id"])["status"] == "completed"

    messages = _wait_message_count(client, session_id, 2)
    by_text = {_message_text(item): item for item in messages}
    user_first = by_text["hello first turn"]
    assistant_first = by_text["PROBE_OK"]
    assert user_first["role"] == "user"
    assert assistant_first["role"] == "assistant"
    for item in (user_first, assistant_first):
        assert item["attribution"]["turn_id"] == first_turn["id"]
        assert item["attribution"]["turn_seq"] == 1
        assert item["attribution"]["model"]["id"] == model_one["id"]
        assert item["attribution"]["agent"]["id"] == primary.id
    assert {body["model"] for body in provider.requests_for("hello first turn")} == {
        "mock-model"
    }

    switched = client.patch(
        f"/api/sessions/{session_id}/selection",
        json={"model_id": model_two["id"]},
        headers=_auth(),
    )
    assert switched.status_code == 200, switched.text
    assert switched.json()["current_agent"]["id"] == primary.id
    assert switched.json()["current_model"]["id"] == model_two["id"]

    before_save = client.portal.call(container.runtime_manager.health, workspace.id)
    before_publication = container.runtime_manager.publication_state(workspace.id)
    assert before_save.phase is RuntimePhase.HEALTHY
    assert before_publication.phase is PublicationPhase.IDLE

    saved = client.put(
        f"/api/models/{model_two['id']}/credential",
        json={"api_key": "probe-key-rotated"},
        headers=_auth(),
    )
    assert saved.status_code == 200, saved.text
    after_save = client.portal.call(container.runtime_manager.health, workspace.id)
    after_save_publication = container.runtime_manager.publication_state(workspace.id)
    assert after_save.phase is RuntimePhase.HEALTHY
    assert after_save.sandbox_id == before_save.sandbox_id
    assert after_save_publication.phase is PublicationPhase.IDLE
    assert (
        after_save_publication.published_projection_hash
        == before_publication.published_projection_hash
    )
    assert after_save_publication.pending_projection_hash is None

    second = client.post(
        f"/api/sessions/{session_id}/turns",
        json={"parts": [{"type": "text", "text": "hello second turn"}]},
        headers=_auth(),
    )
    assert second.status_code == 201, second.text
    second_turn = second.json()
    assert second_turn["turn_seq"] == 2
    assert second_turn["agent"]["id"] == primary.id
    assert second_turn["model"]["id"] == model_two["id"]
    assert _wait_turn(client, session_id, second_turn["id"])["status"] == "completed"

    after_publish = client.portal.call(container.runtime_manager.health, workspace.id)
    after_publish_publication = container.runtime_manager.publication_state(
        workspace.id
    )
    assert after_publish.phase is RuntimePhase.HEALTHY
    assert after_publish.sandbox_id != before_save.sandbox_id
    assert (
        after_publish_publication.published_projection_hash
        != before_publication.published_projection_hash
    )
    configs = list((tmp_path / "root").rglob("opencode/opencode.json"))
    assert configs and all(
        "probe-key-rotated" in path.read_text(encoding="utf-8") for path in configs
    )
    assert {body["model"] for body in provider.requests_for("hello second turn")} == {
        "mock-model-b"
    }

    turns = client.get(f"/api/sessions/{session_id}/turns", headers=_auth())
    assert turns.status_code == 200, turns.text
    attribution = {turn["id"]: turn["model"]["id"] for turn in turns.json()["items"]}
    assert attribution[first_turn["id"]] == model_one["id"]
    assert attribution[second_turn["id"]] == model_two["id"]

    messages = _wait_message_count(client, session_id, 4)
    by_turn: dict[str, list[dict[str, Any]]] = {}
    for item in messages:
        by_turn.setdefault(item["attribution"]["turn_id"], []).append(item)
    assert sorted(item["role"] for item in by_turn[first_turn["id"]]) == [
        "assistant",
        "user",
    ]
    assert sorted(item["role"] for item in by_turn[second_turn["id"]]) == [
        "assistant",
        "user",
    ]
    assert {
        item["attribution"]["model"]["id"] for item in by_turn[first_turn["id"]]
    } == {model_one["id"]}
    assert {
        item["attribution"]["model"]["id"] for item in by_turn[second_turn["id"]]
    } == {model_two["id"]}
    for items in by_turn.values():
        assert {item["attribution"]["agent"]["id"] for item in items} == {primary.id}

    client.portal.call(container.runtime_manager.stop, workspace.id)
    session_after_stop = client.get(f"/api/sessions/{session_id}", headers=_auth())
    assert session_after_stop.status_code == 200
    assert session_after_stop.json()["runtime_status"] == "stopped"

    history = client.get(f"/api/sessions/{session_id}/messages", headers=_auth())
    assert history.status_code == 503
    assert history.json()["error"]["code"] == "RUNTIME_UNAVAILABLE"

    turns_after_stop = client.get(f"/api/sessions/{session_id}/turns", headers=_auth())
    assert turns_after_stop.status_code == 200
    assert len(turns_after_stop.json()["items"]) == 2
