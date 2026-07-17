from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from witty_service.api import backport as backport_api
from witty_service.api.backport_schemas import BackportConfigPayload, BackportRunRequest


class State:
    pass


class RequestStub:
    def __init__(self) -> None:
        self.app = SimpleNamespace(state=State())
        self.app.state.services = MagicMock()


def _service() -> MagicMock:
    service = MagicMock()
    service.config_path = "/tmp/backport/config.json"
    service.get_config.return_value = {
        "project_url": "https://example.com/project",
        "project_dir": "/tmp/project",
        "source_branch": "main",
        "target_path": "/tmp/target",
        "target_release": "v1",
        "patch_dataset_dir": "/tmp/dataset",
        "signer_name": "Witty",
        "signer_email": "witty@example.com",
        "commit_message_template": "{{subject}}",
        "commit_message_source": "manual",
        "linux_repo_path": "/tmp/linux",
        "current_excel_path": "/tmp/current.xlsx",
        "current_report_path": "/tmp/report.md",
        "current_filtered_report_path": "/tmp/filtered.md",
        "commit_sort": "describe",
    }
    service.browse_path.return_value = {"path": "/tmp", "items": []}
    service.run_action.return_value = {
        "agentId": "agent-1",
        "agentName": "Backport",
        "sessionId": "session-1",
        "assistantText": "done",
        "parsedResult": {"ok": True},
        "toolSnapshots": [],
    }
    return service


def test_backport_config_browse_and_run_action() -> None:
    service = _service()

    config = backport_api.get_config(backport_service=service)
    update = backport_api.update_config(
        payload=BackportConfigPayload(project_url="https://example.com/project"),
        backport_service=service,
    )
    browse = backport_api.browse_path(path="/tmp", backport_service=service)
    run = backport_api.run_action(
        payload=BackportRunRequest(action="generate_report", payload={"cve": "1"}),
        backport_service=service,
    )

    assert config.project_url == "https://example.com/project"
    assert update.ok is True
    assert update.config_path == "/tmp/backport/config.json"
    assert browse == {"path": "/tmp", "items": []}
    assert run.agentId == "agent-1"
    service.update_config.assert_called_once()
    service.run_action.assert_called_once_with("generate_report", {"cve": "1"})


def test_create_run_rejects_unsupported_action() -> None:
    with pytest.raises(HTTPException) as exc_info:
        backport_api.create_run(
            payload=BackportRunRequest(action="unknown", payload={}),
            request=RequestStub(),
        )

    assert exc_info.value.status_code == 400


def test_create_and_get_run(monkeypatch) -> None:
    request = RequestStub()
    service = _service()
    monkeypatch.setattr(backport_api, "BackportService", lambda _services, **_kwargs: service)

    class ImmediateThread:
        def __init__(self, target, daemon, name) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    monkeypatch.setattr(backport_api.threading, "Thread", ImmediateThread)

    created = backport_api.create_run(
        payload=BackportRunRequest(action="generate_report", payload={"x": 1}),
        request=request,
    )
    fetched = backport_api.get_run(created.run_id, request=request)

    assert created.status == "success"
    assert fetched.status == "success"
    assert fetched.result["agentId"] == "agent-1"


def test_create_run_sets_paused_at_from_wrapped_parsed_result(monkeypatch) -> None:
    request = RequestStub()
    service = _service()
    service.run_action.return_value = {
        "agentId": "agent-1",
        "agentName": "Backport",
        "sessionId": "session-1",
        "assistantText": "paused",
        "parsedResult": {"stage": "paused"},
        "toolSnapshots": [],
    }
    monkeypatch.setattr(backport_api, "BackportService", lambda _services, **_kwargs: service)
    monkeypatch.setattr(backport_api.time, "time", MagicMock(side_effect=[10.0, 10.0, 20.0, 20.0]))

    class ImmediateThread:
        def __init__(self, target, daemon, name) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    monkeypatch.setattr(backport_api.threading, "Thread", ImmediateThread)

    created = backport_api.create_run(
        payload=BackportRunRequest(action="run_all", payload={"x": 1}),
        request=request,
    )

    assert created.status == "success"
    assert created.paused_at == 20.0
    assert request.app.state.backport_runs[created.run_id]["paused_at"] == 20.0


def test_get_run_raises_when_missing() -> None:
    request = RequestStub()
    request.app.state.backport_runs = {}
    request.app.state.backport_runs_lock = threading.Lock()

    with pytest.raises(HTTPException) as exc_info:
        backport_api.get_run("missing", request=request)

    assert exc_info.value.status_code == 404


def test_pause_run_sets_pause_requested() -> None:
    request = RequestStub()
    request.app.state.backport_runs = {
        "run-1": {
            "run_id": "run-1",
            "action": "run_all",
            "status": "running",
            "result": None,
            "error": "",
            "progress": None,
            "created_at": 1.0,
            "updated_at": 1.0,
            "pause_requested": False,
            "paused_at": None,
        }
    }
    request.app.state.backport_runs_lock = threading.Lock()

    response = backport_api.pause_run("run-1", request=request)

    assert response.run_id == "run-1"
    assert response.pause_requested is True
    assert response.paused_at is None
    assert request.app.state.backport_runs["run-1"]["pause_requested"] is True
    assert request.app.state.backport_runs["run-1"]["updated_at"] >= 1.0


def test_pause_run_is_idempotent_when_already_requested() -> None:
    request = RequestStub()
    request.app.state.backport_runs = {
        "run-1": {
            "run_id": "run-1",
            "action": "run_all",
            "status": "running",
            "result": None,
            "error": "",
            "progress": None,
            "created_at": 1.0,
            "updated_at": 1.0,
            "pause_requested": True,
            "paused_at": None,
        }
    }
    request.app.state.backport_runs_lock = threading.Lock()

    response = backport_api.pause_run("run-1", request=request)

    assert response.pause_requested is True
    assert response.status == "running"


def test_pause_run_rejects_non_run_all_action() -> None:
    request = RequestStub()
    request.app.state.backport_runs = {
        "run-1": {
            "run_id": "run-1",
            "action": "generate_report",
            "status": "running",
            "result": None,
            "error": "",
            "progress": None,
            "created_at": 1.0,
            "updated_at": 1.0,
            "pause_requested": False,
            "paused_at": None,
        }
    }
    request.app.state.backport_runs_lock = threading.Lock()

    with pytest.raises(HTTPException) as exc_info:
        backport_api.pause_run("run-1", request=request)

    assert exc_info.value.status_code == 400


def test_pause_run_returns_finished_record_without_mutating() -> None:
    request = RequestStub()
    request.app.state.backport_runs = {
        "run-1": {
            "run_id": "run-1",
            "action": "run_all",
            "status": "success",
            "result": {"ok": True},
            "error": "",
            "progress": None,
            "created_at": 1.0,
            "updated_at": 2.0,
            "pause_requested": False,
            "paused_at": None,
        }
    }
    request.app.state.backport_runs_lock = threading.Lock()

    response = backport_api.pause_run("run-1", request=request)

    assert response.status == "success"
    assert response.pause_requested is False
    assert request.app.state.backport_runs["run-1"]["updated_at"] == 2.0
