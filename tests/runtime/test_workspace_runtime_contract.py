from __future__ import annotations

from dataclasses import fields, is_dataclass

import pytest

from witty_service.runtime import contracts
from witty_service.runtime.errors import RuntimeContractValidationError


def _request(**overrides: object) -> contracts.TurnExecutionRequest:
    values: dict[str, object] = {
        "workspace_id": "ws_1",
        "session_id": "ses_1",
        "turn_id": "trn_1",
        "runtime_session_id": "ses_rt_1",
        "message_id": "msg_abc",
        "agent_internal_name": "ag_primary",
        "model": contracts.RuntimeModelRef(provider_id="mock", model_id="mock-model"),
        "parts": (contracts.RuntimeTextPart(text="hello"),),
    }
    values.update(overrides)
    return contracts.TurnExecutionRequest(**values)


def _event(**overrides: object) -> contracts.RuntimeEventEnvelope:
    values: dict[str, object] = {
        "workspace_id": "ws_1",
        "runtime_session_id": "ses_rt_1",
        "event_id": "evt_1",
        "type": contracts.RuntimeEventType.MESSAGE_STARTED,
        "durable_seq": 1,
        "occurred_at": 1,
        "payload": contracts.RuntimeEmptyPayload(),
    }
    values.update(overrides)
    return contracts.RuntimeEventEnvelope(**values)


def test_contract_drops_lease_and_config_generation() -> None:
    field_names = {
        item.name
        for value in vars(contracts).values()
        if is_dataclass(value)
        for item in fields(value)
    }
    assert not hasattr(contracts, "RuntimeLease")
    assert not {"lease_id", "config_generation"} & field_names


def test_turn_permit_is_memory_only() -> None:
    permit = contracts.TurnPermit(
        permit_id="prm_1",
        workspace_id="ws_1",
        turn_id="trn_1",
        published_projection_hash="sha256:abc",
    )
    assert {item.name for item in fields(permit)} == {
        "permit_id",
        "workspace_id",
        "turn_id",
        "published_projection_hash",
    }


def test_turn_request_fixes_directory_at_ensure_session() -> None:
    names = {item.name for item in fields(contracts.TurnExecutionRequest)}
    assert not names & {"tools", "directory", "runtime_directory"}
    assert {"agent_internal_name", "model", "parts", "message_id", "resume"} <= names
    assert "runtime_directory" in {
        item.name for item in fields(contracts.RuntimeWorkspace)
    }


def test_resume_is_tri_state() -> None:
    first = _request()
    assert first.resume is None
    assert first.idempotency_key == ("msg_abc", "hello", None, ())

    admit_only = _request(resume=False)
    resumed = _request(resume=True)
    assert (admit_only.resume, resumed.resume) == (False, True)
    assert (
        len(
            {first.idempotency_key, admit_only.idempotency_key, resumed.idempotency_key}
        )
        == 3
    )


def test_text_parts_share_one_idempotency_key() -> None:
    joined = _request(
        parts=(
            contracts.RuntimeTextPart(text="he"),
            contracts.RuntimeTextPart(text="llo"),
        )
    )
    assert joined.text == "hello"
    assert joined.idempotency_key == _request().idempotency_key
    assert (
        _request(parts=(contracts.RuntimeTextPart(text="hello!"),)).idempotency_key
        != joined.idempotency_key
    )
    # message_id 格式由调用方决定，seam 只拒绝空值。
    assert _request(message_id="abc").message_id == "abc"
    with pytest.raises(RuntimeContractValidationError):
        _request(message_id="")


def test_turn_request_requires_text_or_file_parts() -> None:
    with pytest.raises(RuntimeContractValidationError):
        _request(parts=())
    with pytest.raises(RuntimeContractValidationError):
        contracts.RuntimeTextPart(text="")

    part = contracts.RuntimeTextPart(text="hello")
    assert part.to_dict() == {"type": "text", "text": "hello"}

    file_part = contracts.RuntimeFilePart(
        relative_path="docs/a.md", mime="text/markdown", filename="a.md"
    )
    file_request = _request(parts=(contracts.RuntimeTextPart(text="read"), file_part))
    assert file_request.text == "read"
    assert file_request.file_parts == (file_part,)
    assert file_request.idempotency_key[3] == (("docs/a.md", "text/markdown", "a.md"),)

    for unsafe in (
        "/etc/passwd",
        "../secret.txt",
        "C:/Windows/win.ini",
        "docs\\a.md",
        "docs/a\x00.md",
    ):
        with pytest.raises(RuntimeContractValidationError):
            contracts.RuntimeFilePart(relative_path=unsafe, mime="text/plain")

    with pytest.raises(RuntimeContractValidationError):
        _request(
            parts=(
                contracts.RuntimeToolPart(
                    call_id="call_1",
                    name="bash",
                    status=contracts.RuntimeToolStatus.COMPLETED,
                ),
            )
        )


def test_validation_errors_map_to_400() -> None:
    with pytest.raises(RuntimeContractValidationError) as exc:
        contracts.RuntimeTextPart(text=" ")
    assert (exc.value.code, exc.value.status_code) == ("VALIDATION_ERROR", 400)
    assert exc.value.details["field"] == "text"


def test_admission_does_not_imply_started() -> None:
    admission = contracts.TurnAdmission(
        message_id="msg_abc",
        runtime_session_id="ses_rt_1",
        admitted_seq=7,
        delivery="steer",
        admitted_at=1_700_000_000_000,
    )
    assert admission.promoted_seq is None
    assert not hasattr(admission, "status")


def test_event_envelope_uses_durable_seq_for_replay() -> None:
    assert _event(durable_seq=3).durable_seq == 3
    assert contracts.RuntimeEventType.TURN_STARTED.value == "turn.started"
    with pytest.raises(RuntimeContractValidationError):
        _event(durable_seq=-1)
    with pytest.raises(RuntimeContractValidationError):
        _event(type=contracts.RuntimeEventType.TURN_FAILED)


def test_event_payload_shapes_are_normalized() -> None:
    event = _event(
        type=contracts.RuntimeEventType.MESSAGE_DELTA,
        payload=contracts.RuntimeMessageDeltaPayload(delta="hi"),
    )
    assert event.payload.to_dict() == {"delta": "hi"}

    with pytest.raises(RuntimeContractValidationError):
        _event(type=contracts.RuntimeEventType.MESSAGE_DELTA)

    for event_type in contracts.RuntimeEventType:
        assert event_type in contracts.RUNTIME_EVENT_PAYLOAD_TYPES


def test_history_message_parts_are_normalized() -> None:
    message = contracts.RuntimeHistoryMessage(
        message_id="msg_1",
        kind=contracts.RuntimeMessageKind.ASSISTANT,
        created_at=1,
        parts=(
            contracts.RuntimeTextPart(text="hi"),
            contracts.RuntimeReasoningPart(text="thinking"),
            contracts.RuntimeToolPart(
                call_id="call_1",
                name="bash",
                status=contracts.RuntimeToolStatus.COMPLETED,
            ),
            contracts.RuntimeFilePart(relative_path="a.txt", mime="text/plain"),
        ),
    )
    assert [part.to_dict()["type"] for part in message.parts] == [
        "text",
        "reasoning",
        "tool",
        "file",
    ]
    assert message.parts[2].to_dict() == {
        "type": "tool",
        "call_id": "call_1",
        "name": "bash",
        "status": "completed",
    }


def test_history_and_replay_queries_validate_pagination() -> None:
    page = contracts.HistoryPage(items=(), next_cursor="older", previous_cursor="newer")
    assert (page.next_cursor, page.previous_cursor) == ("older", "newer")
    with pytest.raises(RuntimeContractValidationError):
        contracts.HistoryQuery(workspace_id="ws_1", runtime_session_id="ses_1", limit=0)
    with pytest.raises(RuntimeContractValidationError):
        contracts.ReplayQuery(
            workspace_id="ws_1", runtime_session_id="ses_1", after_seq=-1
        )


def test_workspace_snapshot_rejects_mixed_workspaces() -> None:
    health = contracts.RuntimeHealth(workspace_id="ws_1", ready=True)
    location = contracts.RuntimeLocationReadiness(workspace_id="ws_2", ready=True)
    with pytest.raises(RuntimeContractValidationError):
        contracts.RuntimeWorkspaceSnapshot(
            workspace_id="ws_1", health=health, location=location
        )


def test_port_exposes_v2_operations_without_leases() -> None:
    for name in (
        "connect",
        "disconnect",
        "health",
        "inspect_workspace",
        "ensure_session",
        "prepare_turn",
        "admit_turn",
        "subscribe",
        "replay_events",
        "read_history",
        "interrupt_session",
        "stage_revert",
        "commit_revert",
        "clear_revert",
        "delete_session",
        "inspect_session",
    ):
        assert hasattr(contracts.WorkspaceRuntimePort, name)
    for name in ("acquire_lease", "release_lease", "acquire_permit", "release_permit"):
        assert not hasattr(contracts.WorkspaceRuntimePort, name)
