from __future__ import annotations

import logging
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from witty_service.api.services import ServiceContainer
from witty_service.domain.enums import AgentStatus
from witty_service.domain.errors import DomainError
from witty_service.persistence.db import create_session_factory, create_sqlite_engine, init_db
from witty_service.persistence.repositories import SqliteRepository


@pytest.fixture()
def repo() -> SqliteRepository:
    engine = create_sqlite_engine("sqlite:///:memory:")
    init_db(engine, auto_create=True)
    factory = create_session_factory(engine)
    try:
        yield SqliteRepository(factory)
    finally:
        engine.dispose()


def _create_agent(repo: SqliteRepository, agent_id: str, name: str) -> None:
    repo.create_agent_with_id(
        agent_id=agent_id,
        name=name,
        description=f"{name} description",
        sandbox_type="local_process",
        adapter_type="http",
        workspace_path=f"/tmp/{agent_id}",
        idle_timeout_seconds=300,
        status=AgentStatus.running,
    )


def _create_session(
    repo: SqliteRepository,
    *,
    agent_id: str,
    session_id: str,
    runtime_session_id: str | None = None,
) -> None:
    repo.upsert_session(
        session_id=session_id,
        agent_id=agent_id,
        status="idle",
        runtime_type="openclaw",
        runtime_session_key=f"agent:{agent_id}:session:{session_id}",
        remote_runtime_agent_id=f"runtime-agent:{agent_id}",
    )
    if runtime_session_id is not None:
        repo.update_session_runtime_identity(
            session_id=session_id,
            runtime_type="openclaw",
            runtime_session_id=runtime_session_id,
            runtime_session_key=f"agent:{agent_id}:session:{session_id}",
        )


def _http_status_error(method: str, path: str, status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request(method, f"http://localhost:7396{path}")
    response = httpx.Response(status_code, request=request, text="bad gateway")
    return httpx.HTTPStatusError("upstream failure", request=request, response=response)


class FakeInsightHttpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.get_results: dict[str, Any] = {"/health": {"ok": True}}
        self.post_results: dict[str, Any] = {}
        self.delete_results: dict[str, Any] = {}
        self.get_errors: dict[str, Exception] = {}
        self.post_errors: dict[str, Exception] = {}
        self.delete_errors: dict[str, Exception] = {}
        self.base_url = "http://localhost:7396"
        self._timeout = 5.0

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path, params))
        error = self.get_errors.get(path)
        if error is not None:
            raise error
        return self.get_results.get(path)

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append(("POST", path, {"json": json, "timeout": timeout}))
        error = self.post_errors.get(path)
        if error is not None:
            raise error
        return self.post_results.get(path)

    async def delete(self, path: str) -> Any:
        self.calls.append(("DELETE", path, None))
        error = self.delete_errors.get(path)
        if error is not None:
            raise error
        return self.delete_results.get(path)


def _make_facade(
    repo: SqliteRepository,
    insight_http_client: FakeInsightHttpClient | None = None,
):
    from witty_service.application.insight_facade import InsightFacade

    return InsightFacade(
        ServiceContainer(
            repository=repo,
            workspace_store=MagicMock(),
            insight_http_client=insight_http_client,
        ),
    )


def test_raw_params_uses_bracketed_session_ids_for_multiple_runtime_sessions() -> None:
    from witty_service.application.insight_facade import InsightFacade

    assert InsightFacade._raw_params(session_ids=["runtime-1", "runtime-2"]) == [
        ("session_ids[]", "runtime-1"),
        ("session_ids[]", "runtime-2"),
    ]


def test_raw_params_keeps_singular_session_id_for_single_runtime_session() -> None:
    from witty_service.application.insight_facade import InsightFacade

    assert InsightFacade._raw_params(session_ids=["runtime-1"]) == {
        "session_id": "runtime-1",
    }


@pytest.mark.asyncio
async def test_get_capabilities_reports_unreachable_when_health_probe_fails(
    repo: SqliteRepository,
) -> None:
    insight_http_client = FakeInsightHttpClient()
    insight_http_client.get_errors["/health"] = httpx.ConnectError("connection refused")
    facade = _make_facade(repo, insight_http_client)

    assert await facade.get_capabilities() == {
        "enabled": True,
        "reachable": False,
        "features": {
            "sessions": True,
            "timeseries": True,
            "interruptions": True,
            "health": True,
        },
    }


@pytest.mark.asyncio
async def test_list_sessions_enriches_managed_sessions_and_warns_for_missing_links(
    repo: SqliteRepository,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _create_agent(repo, "agent-1", "Alpha")
    _create_agent(repo, "agent-2", "Beta")
    _create_session(repo, agent_id="agent-1", session_id="session-1", runtime_session_id="runtime-1")
    _create_session(repo, agent_id="agent-2", session_id="session-2", runtime_session_id="runtime-2")
    repo.list_agent_records_by_ids = MagicMock(
        return_value=repo.list_agent_records_by_ids(["agent-1"])
    )

    insight_http_client = FakeInsightHttpClient()
    insight_http_client.get_results["/api/sessions"] = [
        {
            "session_id": "runtime-1",
            "conversation_count": 3,
            "first_seen_ns": 100,
            "last_seen_ns": 300,
            "total_input_tokens": 11,
            "total_output_tokens": 7,
            "model": "gpt-4o",
            "agent_name": "raw-alpha",
        },
        {
            "session_id": "runtime-2",
            "conversation_count": 2,
            "first_seen_ns": 200,
            "last_seen_ns": 400,
            "total_input_tokens": 5,
            "total_output_tokens": 6,
            "model": "gpt-4o-mini",
            "agent_name": "raw-beta",
        },
        {
            "session_id": "runtime-missing",
            "conversation_count": 1,
            "first_seen_ns": 500,
            "last_seen_ns": 600,
            "total_input_tokens": 2,
            "total_output_tokens": 3,
            "model": "gpt-4.1",
        },
    ]
    facade = _make_facade(repo, insight_http_client)

    with caplog.at_level(logging.WARNING):
        result = await facade.list_sessions()

    assert insight_http_client.calls[-1] == (
        "GET",
        "/api/sessions",
        [
            ("session_ids[]", "runtime-1"),
            ("session_ids[]", "runtime-2"),
        ],
    )
    assert result == [
        {
            "session_id": "session-1",
            "runtime_session_id": "runtime-1",
            "witty_agent_id": "agent-1",
            "witty_agent_name": "Alpha",
            "agent_name": "Alpha",
            "conversation_count": 3,
            "first_seen_ns": 100,
            "last_seen_ns": 300,
            "total_input_tokens": 11,
            "total_output_tokens": 7,
            "model": "gpt-4o",
        }
    ]
    assert "runtime session is missing a local witty session mapping" in caplog.text
    assert "mapped witty session references a missing agent" in caplog.text


@pytest.mark.asyncio
async def test_get_session_traces_uses_witty_session_runtime_mapping(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo, "agent-1", "Alpha")
    _create_session(repo, agent_id="agent-1", session_id="session-1", runtime_session_id="runtime-1")
    insight_http_client = FakeInsightHttpClient()
    insight_http_client.get_results["/api/sessions/runtime-1/traces"] = [{"trace_id": "trace-1"}]
    facade = _make_facade(repo, insight_http_client)

    result = await facade.get_session_traces("session-1", start_ns=10, end_ns=20)

    assert insight_http_client.calls[-1] == (
        "GET",
        "/api/sessions/runtime-1/traces",
        {"start_ns": 10, "end_ns": 20},
    )
    assert result == [{"trace_id": "trace-1"}]


@pytest.mark.asyncio
async def test_get_session_traces_raises_when_runtime_mapping_is_missing(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo, "agent-1", "Alpha")
    _create_session(repo, agent_id="agent-1", session_id="session-1")
    facade = _make_facade(repo, FakeInsightHttpClient())

    with pytest.raises(DomainError) as exc_info:
        await facade.get_session_traces("session-1")

    assert exc_info.value.code == "INSIGHT_SESSION_MAPPING_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_session_interruptions_remaps_runtime_session_ids(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo, "agent-1", "Alpha")
    _create_session(repo, agent_id="agent-1", session_id="session-1", runtime_session_id="runtime-1")
    insight_http_client = FakeInsightHttpClient()
    insight_http_client.get_results["/api/sessions/runtime-1/interruptions"] = [
        {
            "interruption_id": "interrupt-1",
            "session_id": "runtime-1",
            "severity": "critical",
        }
    ]
    facade = _make_facade(repo, insight_http_client)

    result = await facade.get_session_interruptions("session-1")

    assert result == [
        {
            "interruption_id": "interrupt-1",
            "session_id": "session-1",
            "runtime_session_id": "runtime-1",
            "severity": "critical",
        }
    ]


@pytest.mark.asyncio
async def test_get_timeseries_and_interruption_count_return_empty_without_managed_sessions(
    repo: SqliteRepository,
) -> None:
    facade = _make_facade(repo, FakeInsightHttpClient())

    assert await facade.get_timeseries() == {"token_series": [], "model_series": []}
    assert await facade.get_interruption_count() == {
        "total": 0,
        "by_severity": {"critical": 0, "high": 0, "medium": 0, "low": 0},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "args", "http_method", "path", "error", "expected_code"),
    [
        (
            "get_trace_detail",
            ("trace-1",),
            "GET",
            "/api/traces/trace-1",
            httpx.ConnectError("boom"),
            "INSIGHT_UNAVAILABLE",
        ),
        (
            "resolve_interruption",
            ("interrupt-1",),
            "POST",
            "/api/interruptions/interrupt-1/resolve",
            httpx.ReadTimeout("slow"),
            "INSIGHT_TIMEOUT",
        ),
        (
            "delete_agent_health",
            (101,),
            "DELETE",
            "/api/agent-health/101",
            _http_status_error("DELETE", "/api/agent-health/101", 503),
            "INSIGHT_UPSTREAM_ERROR",
        ),
        (
            "restart_agent_health",
            (101,),
            "POST",
            "/api/agent-health/101/restart",
            ValueError("invalid json"),
            "INSIGHT_BAD_RESPONSE",
        ),
    ],
)
async def test_insight_http_errors_are_mapped_to_domain_errors(
    repo: SqliteRepository,
    method_name: str,
    args: tuple[Any, ...],
    http_method: str,
    path: str,
    error: Exception,
    expected_code: str,
) -> None:
    insight_http_client = FakeInsightHttpClient()
    if http_method == "GET":
        insight_http_client.get_errors[path] = error
    elif http_method == "POST":
        insight_http_client.post_errors[path] = error
    else:
        insight_http_client.delete_errors[path] = error

    facade = _make_facade(repo, insight_http_client)

    with pytest.raises(DomainError) as exc_info:
        await getattr(facade, method_name)(*args)

    assert exc_info.value.code == expected_code
    assert exc_info.value.details["path"] == path
    assert exc_info.value.details["base_url"] == "http://localhost:7396"


@pytest.mark.asyncio
async def test_export_atif_session_rewrites_session_identity(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo, "agent-1", "Alpha")
    _create_session(repo, agent_id="agent-1", session_id="session-1", runtime_session_id="runtime-1")
    insight_http_client = FakeInsightHttpClient()
    insight_http_client.get_results["/api/export/atif/session/runtime-1"] = {
        "schema_version": "1.6",
        "session_id": "runtime-1",
        "agent": {},
        "steps": [],
    }
    facade = _make_facade(repo, insight_http_client)

    result = await facade.export_atif_session("session-1")

    assert result == {
        "schema_version": "1.6",
        "session_id": "session-1",
        "runtime_session_id": "runtime-1",
        "agent": {},
        "steps": [],
    }


@pytest.mark.asyncio
async def test_get_agent_health_joins_managed_agents_and_orphan_runtimes(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo, "agent-1", "Alpha")
    _create_agent(repo, "agent-2", "Beta")
    repo.save_sandbox_state(
        "agent-1",
        sandbox_payload_json={
            "sandbox_id": "sandbox-1",
            "workspace_path": "/tmp/agent-1",
            "metadata": {"gateway_port": 18080, "stderr_log_path": "/tmp/alpha.err"},
        },
        adapter_base_url="http://127.0.0.1:18080",
        adapter_ready=True,
    )
    repo.save_sandbox_state(
        "agent-2",
        sandbox_payload_json={
            "sandbox_id": "sandbox-2",
            "workspace_path": "/tmp/agent-2",
            "metadata": {"gateway_port": 18081},
        },
        adapter_base_url="http://127.0.0.1:18081",
        adapter_ready=False,
    )

    insight_http_client = FakeInsightHttpClient()
    insight_http_client.get_results["/api/agent-health"] = {
        "agents": [
            {
                "pid": 101,
                "agent_name": "raw-alpha",
                "category": "openclaw",
                "exe_path": "/usr/bin/openclaw",
                "ports": [18080],
                "status": "healthy",
                "last_check_time": 111,
                "latency_ms": 12,
                "error_message": None,
            },
            {
                "pid": 202,
                "agent_name": "orphan-runtime",
                "category": "openclaw",
                "exe_path": "/usr/bin/openclaw",
                "ports": [19999],
                "status": "offline",
                "last_check_time": 222,
                "latency_ms": None,
                "error_message": "exited",
            },
        ],
        "last_scan_time": 999,
    }
    facade = _make_facade(repo, insight_http_client)

    result = await facade.get_agent_health()

    assert result["last_scan_time"] == 999
    assert result["agents"] == [
        {
            "witty_agent_id": "agent-1",
            "witty_agent_name": "Alpha",
            "witty_status": "running",
            "overall_status": "healthy",
            "status_reason": None,
            "adapter_type": "http",
            "sandbox_type": "local_process",
            "workspace_path": "/tmp/agent-1",
            "gateway_port": 18080,
            "adapter_base_url": "http://127.0.0.1:18080",
            "adapter_ready": True,
            "adapter_status": "ready",
            "adapter_latency_ms": 12,
            "adapter_error_message": None,
            "adapter_pid": 101,
            "stderr_log_path": "/tmp/alpha.err",
            "runtime": {
                "pid": 101,
                "agent_name": "raw-alpha",
                "category": "openclaw",
                "exe_path": "/usr/bin/openclaw",
                "ports": [18080],
                "status": "healthy",
                "last_check_time": 111,
                "latency_ms": 12,
                "error_message": None,
            },
            "candidate_runtimes": [],
        },
        {
            "witty_agent_id": "agent-2",
            "witty_agent_name": "Beta",
            "witty_status": "running",
            "overall_status": "missing_runtime",
            "status_reason": "No matching runtime health entry found.",
            "adapter_type": "http",
            "sandbox_type": "local_process",
            "workspace_path": "/tmp/agent-2",
            "gateway_port": 18081,
            "adapter_base_url": "http://127.0.0.1:18081",
            "adapter_ready": False,
            "adapter_status": "not_ready",
            "adapter_latency_ms": None,
            "adapter_error_message": None,
            "adapter_pid": None,
            "stderr_log_path": None,
            "runtime": None,
            "candidate_runtimes": [],
        },
    ]
    assert result["orphan_runtimes"] == [
        {
            "pid": 202,
            "agent_name": "orphan-runtime",
            "category": "openclaw",
            "exe_path": "/usr/bin/openclaw",
            "ports": [19999],
            "status": "offline",
            "last_check_time": 222,
            "latency_ms": None,
            "error_message": "exited",
        }
    ]


@pytest.mark.asyncio
async def test_get_agent_health_prefers_live_runtime_over_stale_offline_matches(
    repo: SqliteRepository,
) -> None:
    _create_agent(repo, "agent-1", "Alpha")
    repo.save_sandbox_state(
        "agent-1",
        sandbox_payload_json={
            "sandbox_id": "sandbox-1",
            "workspace_path": "/tmp/agent-1",
            "metadata": {"gateway_port": 18080, "stderr_log_path": "/tmp/alpha.err"},
        },
        adapter_base_url="http://127.0.0.1:18080",
        adapter_ready=True,
    )

    insight_http_client = FakeInsightHttpClient()
    insight_http_client.get_results["/api/agent-health"] = {
        "agents": [
            {
                "pid": 101,
                "agent_name": "raw-alpha-live",
                "category": "openclaw",
                "exe_path": "/usr/bin/openclaw",
                "ports": [18080],
                "status": "healthy",
                "last_check_time": 300,
                "latency_ms": 8,
                "error_message": None,
            },
            {
                "pid": 99,
                "agent_name": "raw-alpha-old",
                "category": "openclaw",
                "exe_path": "/usr/bin/openclaw",
                "ports": [18080],
                "status": "offline",
                "last_check_time": 200,
                "latency_ms": None,
                "error_message": "exited",
            },
        ],
        "last_scan_time": 1234,
    }
    facade = _make_facade(repo, insight_http_client)

    result = await facade.get_agent_health()
    agent = result["agents"][0]

    assert agent["overall_status"] == "healthy"
    assert agent["status_reason"] is None
    assert agent["adapter_pid"] == 101
    assert agent["runtime"] == {
        "pid": 101,
        "agent_name": "raw-alpha-live",
        "category": "openclaw",
        "exe_path": "/usr/bin/openclaw",
        "ports": [18080],
        "status": "healthy",
        "last_check_time": 300,
        "latency_ms": 8,
        "error_message": None,
    }
    assert agent["candidate_runtimes"] == []
