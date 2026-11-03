from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from witty_service.domain.errors import DomainError
from witty_service.main import create_app

AUTH_HEADERS = {"Authorization": "Bearer test-token"}


def _client_with_facade(monkeypatch, facade: MagicMock) -> TestClient:
    monkeypatch.setenv("AUTH_TOKEN", "test-token")
    services = MagicMock()
    services.get_insight_facade.return_value = facade
    services.repository = MagicMock()
    services.repository.find_stale_generating_messages.return_value = []
    services.repository.list_agents_needing_recovery.return_value = []
    services.close = AsyncMock()
    return TestClient(create_app(services=services))


def _session_payload() -> dict[str, Any]:
    return {
        "session_id": "session-1",
        "runtime_session_id": "runtime-1",
        "witty_agent_id": "agent-1",
        "witty_agent_name": "Alpha",
        "agent_name": "Alpha",
        "conversation_count": 1,
        "first_seen_ns": 100,
        "last_seen_ns": 200,
        "total_input_tokens": 10,
        "total_output_tokens": 5,
        "model": "gpt-4o",
    }


def _trace_summary_payload() -> dict[str, Any]:
    return {
        "trace_id": "trace-1",
        "conversation_id": "conv-1",
        "call_count": 2,
        "total_input_tokens": 10,
        "total_output_tokens": 5,
        "start_ns": 100,
        "end_ns": 200,
        "model": "gpt-4o",
        "user_query": "hello",
    }


def _trace_detail_payload() -> dict[str, Any]:
    return {
        "id": 1,
        "call_id": "call-1",
        "start_timestamp_ns": 100,
        "end_timestamp_ns": 200,
        "model": "gpt-4o",
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "input_messages": "in",
        "output_messages": "out",
        "system_instructions": "sys",
        "agent_name": "Alpha",
        "process_name": "opencode",
        "pid": 123,
        "user_query": "hello",
        "event_json": "{}",
        "trace_id": "trace-1",
        "conversation_id": "conv-1",
        "cache_read_tokens": 0,
        "status": "ok",
        "interruption_type": None,
    }


def _interruption_record_payload() -> dict[str, Any]:
    return {
        "id": 1,
        "interruption_id": "interrupt-1",
        "session_id": "session-1",
        "runtime_session_id": "runtime-1",
        "trace_id": "trace-1",
        "conversation_id": "conv-1",
        "call_id": "call-1",
        "pid": 123,
        "agent_name": "Alpha",
        "interruption_type": "agent_crash",
        "severity": "critical",
        "occurred_at_ns": 100,
        "detail": "crashed",
        "resolved": False,
    }


def _timeseries_payload() -> dict[str, Any]:
    return {
        "token_series": [
            {
                "bucket_start_ns": 100,
                "input_tokens": 1,
                "output_tokens": 2,
                "total_tokens": 3,
            }
        ],
        "model_series": [
            {
                "bucket_start_ns": 100,
                "model": "gpt-4o",
                "total_tokens": 3,
            }
        ],
    }


def _interruption_count_payload() -> dict[str, Any]:
    return {
        "total": 2,
        "by_severity": {"critical": 1, "high": 1, "medium": 0, "low": 0},
    }


def _interruption_session_counts_payload() -> list[dict[str, Any]]:
    return [
        {
            "session_id": "session-1",
            "runtime_session_id": "runtime-1",
            "total": 2,
            "by_severity": {"critical": 1, "high": 1, "medium": 0, "low": 0},
            "types": [
                {
                    "interruption_type": "agent_crash",
                    "severity": "critical",
                    "count": 1,
                }
            ],
        }
    ]


def _agent_health_payload() -> dict[str, Any]:
    return {
        "agents": [
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
                    "category": "opencode",
                    "exe_path": "/usr/bin/opencode",
                    "ports": [18080],
                    "status": "healthy",
                    "last_check_time": 111,
                    "latency_ms": 12,
                    "error_message": None,
                },
                "candidate_runtimes": [],
            }
        ],
        "orphan_runtimes": [
            {
                "pid": 202,
                "agent_name": "orphan-runtime",
                "category": "opencode",
                "exe_path": "/usr/bin/opencode",
                "ports": [19999],
                "status": "offline",
                "last_check_time": 222,
                "latency_ms": None,
                "error_message": "exited",
            }
        ],
        "last_scan_time": 999,
    }


def _atif_session_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.6",
        "session_id": "session-1",
        "runtime_session_id": "runtime-1",
        "agent": {"name": "Alpha", "version": "test"},
        "steps": [{"step_id": 1, "source": "user", "message": "hello"}],
        "final_metrics": None,
        "extra": None,
    }


def _atif_conversation_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.6",
        "session_id": "conv-1",
        "runtime_session_id": None,
        "agent": {"name": "Alpha", "version": "test"},
        "steps": [{"step_id": 1, "source": "user", "message": "hello"}],
        "final_metrics": None,
        "extra": None,
    }


@pytest.mark.parametrize(
    (
        "request_method",
        "path",
        "params",
        "facade_method",
        "payload",
        "expected_args",
        "expected_kwargs",
    ),
    [
        (
            "get",
            "/insight/capabilities",
            None,
            "get_capabilities",
            {
                "enabled": False,
                "reachable": False,
                "features": {
                    "sessions": False,
                    "timeseries": False,
                    "interruptions": False,
                    "health": False,
                },
            },
            (),
            {},
        ),
        (
            "get",
            "/insight/sessions",
            {"witty_agent_id": "agent-1", "start_ns": 100, "end_ns": 200},
            "list_sessions",
            [_session_payload()],
            (),
            {"witty_agent_id": "agent-1", "start_ns": 100, "end_ns": 200},
        ),
        (
            "get",
            "/insight/timeseries",
            {"witty_agent_id": "agent-1", "start_ns": 100, "end_ns": 200, "buckets": 5},
            "get_timeseries",
            _timeseries_payload(),
            (),
            {"witty_agent_id": "agent-1", "start_ns": 100, "end_ns": 200, "buckets": 5},
        ),
        (
            "get",
            "/insight/sessions/session-1/traces",
            {"start_ns": 100, "end_ns": 200},
            "get_session_traces",
            [_trace_summary_payload()],
            ("session-1",),
            {"start_ns": 100, "end_ns": 200},
        ),
        (
            "get",
            "/insight/traces/trace-1",
            None,
            "get_trace_detail",
            [_trace_detail_payload()],
            ("trace-1",),
            {},
        ),
        (
            "get",
            "/insight/sessions/session-1/interruptions",
            None,
            "get_session_interruptions",
            [_interruption_record_payload()],
            ("session-1",),
            {},
        ),
        (
            "get",
            "/insight/conversations/conv-1/interruptions",
            None,
            "get_conversation_interruptions",
            [_interruption_record_payload()],
            ("conv-1",),
            {},
        ),
        (
            "post",
            "/insight/interruptions/interrupt-1/resolve",
            None,
            "resolve_interruption",
            {"status": "resolved"},
            ("interrupt-1",),
            {},
        ),
        (
            "get",
            "/insight/interruptions/session-counts",
            None,
            "get_interruption_session_counts",
            _interruption_session_counts_payload(),
            (),
            {"witty_agent_id": None, "start_ns": None, "end_ns": None},
        ),
        (
            "get",
            "/insight/interruptions/count",
            {"witty_agent_id": "agent-1", "start_ns": 100, "end_ns": 200},
            "get_interruption_count",
            _interruption_count_payload(),
            (),
            {"witty_agent_id": "agent-1", "start_ns": 100, "end_ns": 200},
        ),
        (
            "get",
            "/insight/agent-health",
            None,
            "get_agent_health",
            _agent_health_payload(),
            (),
            {},
        ),
        (
            "delete",
            "/insight/agent-health/101",
            None,
            "delete_agent_health",
            {"ok": True},
            (101,),
            {},
        ),
        (
            "post",
            "/insight/agent-health/101/restart",
            None,
            "restart_agent_health",
            {"ok": True, "new_pid": 202, "cmd": ["python", "agent.py"]},
            (101,),
            {},
        ),
        (
            "get",
            "/insight/export/atif/session/session-1",
            None,
            "export_atif_session",
            _atif_session_payload(),
            ("session-1",),
            {},
        ),
        (
            "get",
            "/insight/export/atif/conversation/conv-1",
            None,
            "export_atif_conversation",
            _atif_conversation_payload(),
            ("conv-1",),
            {},
        ),
    ],
)
def test_insight_routes_return_facade_payloads(
    monkeypatch,
    request_method: str,
    path: str,
    params: dict[str, Any] | None,
    facade_method: str,
    payload: Any,
    expected_args: tuple[Any, ...],
    expected_kwargs: dict[str, Any],
) -> None:
    facade = MagicMock()
    mocked_method = AsyncMock(return_value=payload)
    setattr(facade, facade_method, mocked_method)
    client = _client_with_facade(monkeypatch, facade)

    request = getattr(client, request_method)
    resp = request(path, headers=AUTH_HEADERS, params=params)

    assert resp.status_code == 200
    assert resp.json() == payload
    mocked_method.assert_awaited_once_with(*expected_args, **expected_kwargs)


def test_domain_error_is_returned_in_standard_error_shape(monkeypatch) -> None:
    facade = MagicMock()
    facade.get_session_traces = AsyncMock(
        side_effect=DomainError(
            code="INSIGHT_SESSION_MAPPING_NOT_FOUND",
            message="witty session is not mapped to a runtime insight session",
            status_code=404,
            details={"session_id": "missing"},
        )
    )
    client = _client_with_facade(monkeypatch, facade)

    resp = client.get(
        "/insight/sessions/missing/traces",
        headers=AUTH_HEADERS,
    )

    assert resp.status_code == 404
    assert resp.json() == {
        "error": {
            "code": "INSIGHT_SESSION_MAPPING_NOT_FOUND",
            "message": "witty session is not mapped to a runtime insight session",
            "details": {"session_id": "missing"},
        }
    }
