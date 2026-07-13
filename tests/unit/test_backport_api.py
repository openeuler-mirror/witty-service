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
    monkeypatch.setattr(backport_api, "BackportService", lambda _services: service)

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


def test_get_run_raises_when_missing() -> None:
    request = RequestStub()
    request.app.state.backport_runs = {}
    request.app.state.backport_runs_lock = threading.Lock()

    with pytest.raises(HTTPException) as exc_info:
        backport_api.get_run("missing", request=request)

    assert exc_info.value.status_code == 404


# ── target_config_layout / target_config_layout_opts Schema 测试 ──

from witty_service.api.backport_schemas import TargetConfigLayoutOpts


class TestTargetConfigLayoutSchema:
    def test_default_values(self) -> None:
        """默认值：layout=none, opts.default_level=L1-RECOMMEND"""
        payload = BackportConfigPayload()
        assert payload.target_config_layout == "none"
        assert payload.target_config_layout_opts.default_level == "L1-RECOMMEND"

    def test_anolis_with_l2_optional_accepted(self) -> None:
        """合法组合 anolis + L2-OPTIONAL 被接受"""
        payload = BackportConfigPayload(
            target_config_layout="anolis",
            target_config_layout_opts={"default_level": "L2-OPTIONAL"},
        )
        assert payload.target_config_layout == "anolis"
        assert payload.target_config_layout_opts.default_level == "L2-OPTIONAL"

    def test_invalid_layout_rejected(self) -> None:
        """非法 layout 值被 Pydantic 拒绝"""
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            BackportConfigPayload(target_config_layout="invalid")

    def test_invalid_default_level_rejected(self) -> None:
        """非法 default_level 值被 Pydantic 拒绝"""
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            BackportConfigPayload(
                target_config_layout_opts={"default_level": "INVALID"},
            )

    def test_target_config_layout_opts_default_factory(self) -> None:
        """default_factory 创建独立实例"""
        opts = TargetConfigLayoutOpts()
        assert opts.default_level == "L1-RECOMMEND"

    def test_target_config_layout_opts_all_valid_levels(self) -> None:
        """三个合法 default_level 值都被接受"""
        for level in ("L0-MANDATORY", "L1-RECOMMEND", "L2-OPTIONAL"):
            opts = TargetConfigLayoutOpts(default_level=level)
            assert opts.default_level == level
