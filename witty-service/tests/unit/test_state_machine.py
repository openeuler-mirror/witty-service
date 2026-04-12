import pytest

from witty_service.domain.enums import AgentStatus, can_transition
from witty_service.domain.errors import DomainError


@pytest.mark.parametrize(
    ("from_status", "to_status", "expected"),
    [
        (AgentStatus.creating, AgentStatus.running, True),
        (AgentStatus.creating, AgentStatus.error, True),
        (AgentStatus.creating, AgentStatus.paused, False),
        (AgentStatus.running, AgentStatus.stopped, True),
        (AgentStatus.running, AgentStatus.error, True),
        (AgentStatus.paused, AgentStatus.running, True),
        (AgentStatus.paused, AgentStatus.stopped, True),
        (AgentStatus.paused, AgentStatus.error, True),
        (AgentStatus.error, AgentStatus.stopped, True),
        (AgentStatus.error, AgentStatus.running, True),
        (AgentStatus.stopped, AgentStatus.running, False),
    ],
)
def test_can_transition_state_matrix(from_status, to_status, expected):
    assert can_transition(from_status, to_status) is expected


def test_can_transition_running_to_paused():
    assert can_transition(AgentStatus.running, AgentStatus.paused) is True


def test_can_transition_paused_to_creating():
    assert can_transition(AgentStatus.paused, AgentStatus.creating) is False


def test_can_transition_stopped_to_running_is_invalid():
    assert can_transition(AgentStatus.stopped, AgentStatus.running) is False


def test_can_transition_rejects_non_status_input():
    assert can_transition(123, AgentStatus.running) is False


def test_domain_error_copies_initial_details_independently():
    details = {"from": "paused", "meta": {"attempts": 1}}
    err = DomainError(code="INVALID_STATUS", message="invalid status", details=details)
    details["from"] = "running"
    details["meta"]["attempts"] = 2

    assert err.details == {"from": "paused", "meta": {"attempts": 1}}


def test_domain_error_exposes_code_message_and_details():
    err = DomainError(
        code="INVALID_STATUS",
        message="invalid status",
        details={"from": "paused", "meta": {"attempts": 1}},
    )

    assert err.code == "INVALID_STATUS"
    assert err.message == "invalid status"
    assert err.details == {"from": "paused", "meta": {"attempts": 1}}
    assert str(err) == "invalid status"

    payload = err.to_payload()
    payload_dict = payload.to_dict()

    payload_dict["details"]["meta"]["attempts"] = 2
    payload_dict["details"]["from"] = "running"

    assert err.details == {"from": "paused", "meta": {"attempts": 1}}
    assert payload.details == {"from": "paused", "meta": {"attempts": 1}}
