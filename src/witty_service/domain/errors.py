from __future__ import annotations

from copy import deepcopy
from typing import Any

from witty_service.domain.models import ErrorPayload

INSIGHT_DISABLED = "INSIGHT_DISABLED"
INSIGHT_UNAVAILABLE = "INSIGHT_UNAVAILABLE"
INSIGHT_TIMEOUT = "INSIGHT_TIMEOUT"
INSIGHT_UPSTREAM_ERROR = "INSIGHT_UPSTREAM_ERROR"
INSIGHT_BAD_RESPONSE = "INSIGHT_BAD_RESPONSE"
INSIGHT_SESSION_MAPPING_NOT_FOUND = "INSIGHT_SESSION_MAPPING_NOT_FOUND"
SESSION_NOT_FOUND = "SESSION_NOT_FOUND"


class DomainError(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = deepcopy(details or {})

    def to_payload(self) -> ErrorPayload:
        return ErrorPayload(
            code=self.code,
            message=self.message,
            details=deepcopy(self.details),
        )

    def __str__(self) -> str:
        return self.message


class AgentServiceError(DomainError):
    pass


class InvalidAgentTransitionError(AgentServiceError):
    def __init__(self, *, current: str, target: str) -> None:
        super().__init__(
            code="INVALID_AGENT_TRANSITION",
            message="invalid agent state transition",
            status_code=400,
            details={"current": current, "target": target},
        )


class AgentConfigUpdateForbiddenError(AgentServiceError):
    def __init__(self) -> None:
        super().__init__(
            code="AGENT_CONFIG_UPDATE_FORBIDDEN",
            message="cannot update agent config while running",
            status_code=409,
        )


class InvalidAgentConfigError(AgentServiceError):
    def __init__(self) -> None:
        super().__init__(
            code="INVALID_AGENT_CONFIG",
            message="invalid agent config",
            status_code=400,
        )


class AgentIdNotConfiguredError(AgentServiceError):
    def __init__(self, *, agent_id: str, configured_ids: list[str]) -> None:
        super().__init__(
            code="AGENT_ID_NOT_CONFIGURED",
            message="agent id is not configured in openclaw agents.list",
            status_code=400,
            details={"agent_id": agent_id, "configured_ids": configured_ids},
        )


class AgentDefaultNotConfiguredError(AgentServiceError):
    def __init__(self) -> None:
        super().__init__(
            code="AGENT_DEFAULT_NOT_CONFIGURED",
            message="default agent is not configured in openclaw agents.list",
            status_code=500,
        )


class AgentContextMismatchError(AgentServiceError):
    def __init__(
        self, *, requested_agent_id: str, current_agent_id: str | None
    ) -> None:
        super().__init__(
            code="AGENT_CONTEXT_MISMATCH",
            message="requested agent id does not match current agent context",
            status_code=409,
            details={
                "requested_agent_id": requested_agent_id,
                "current_agent_id": current_agent_id,
            },
        )


class OpenClawAgentNotFoundError(AgentServiceError):
    def __init__(self, *, agent_id: str) -> None:
        super().__init__(
            code="OPENCLAW_AGENT_NOT_FOUND",
            message="openclaw gateway did not load configured agent",
            status_code=500,
            details={"agent_id": agent_id},
        )


def insight_disabled() -> DomainError:
    return DomainError(
        code=INSIGHT_DISABLED,
        message="witty insight integration is disabled",
        status_code=503,
    )


def insight_unavailable(*, base_url: str, path: str, reason: str) -> DomainError:
    return DomainError(
        code=INSIGHT_UNAVAILABLE,
        message="witty insight is unavailable",
        status_code=503,
        details={"base_url": base_url, "path": path, "reason": reason},
    )


def insight_timeout(*, base_url: str, path: str, timeout_seconds: float) -> DomainError:
    return DomainError(
        code=INSIGHT_TIMEOUT,
        message="witty insight request timed out",
        status_code=504,
        details={
            "base_url": base_url,
            "path": path,
            "timeout_seconds": timeout_seconds,
        },
    )


def insight_upstream_error(
    *,
    base_url: str,
    path: str,
    status_code: int,
    response_text: str,
) -> DomainError:
    return DomainError(
        code=INSIGHT_UPSTREAM_ERROR,
        message="witty insight upstream request failed",
        status_code=502,
        details={
            "base_url": base_url,
            "path": path,
            "status_code": status_code,
            "response_text": response_text,
        },
    )


def insight_bad_response(*, base_url: str, path: str, reason: str) -> DomainError:
    return DomainError(
        code=INSIGHT_BAD_RESPONSE,
        message="witty insight returned an invalid response",
        status_code=502,
        details={"base_url": base_url, "path": path, "reason": reason},
    )


def insight_session_mapping_not_found(
    *,
    session_id: str,
    runtime_type: str | None,
    runtime_session_id: str | None,
) -> DomainError:
    return DomainError(
        code=INSIGHT_SESSION_MAPPING_NOT_FOUND,
        message="witty session is not mapped to a runtime insight session",
        status_code=404,
        details={
            "session_id": session_id,
            "runtime_type": runtime_type,
            "runtime_session_id": runtime_session_id,
        },
    )


def session_not_found(
    *,
    session_id: str,
    agent_id: str | None = None,
) -> DomainError:
    details = {"session_id": session_id}
    if agent_id is not None:
        details["agent_id"] = agent_id
    return DomainError(
        code=SESSION_NOT_FOUND,
        message="Session was not found.",
        status_code=404,
        details=details,
    )
