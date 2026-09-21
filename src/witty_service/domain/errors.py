from __future__ import annotations

from copy import deepcopy
from typing import Any, ClassVar

from witty_service.domain.models import ErrorPayload

INSIGHT_DISABLED = "INSIGHT_DISABLED"
INSIGHT_UNAVAILABLE = "INSIGHT_UNAVAILABLE"
INSIGHT_TIMEOUT = "INSIGHT_TIMEOUT"
INSIGHT_UPSTREAM_ERROR = "INSIGHT_UPSTREAM_ERROR"
INSIGHT_BAD_RESPONSE = "INSIGHT_BAD_RESPONSE"
INSIGHT_SESSION_MAPPING_NOT_FOUND = "INSIGHT_SESSION_MAPPING_NOT_FOUND"
SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
AGENT_NOT_FOUND = "AGENT_NOT_FOUND"
INVALID_SESSION_METADATA = "INVALID_SESSION_METADATA"


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


class FieldValidationError(DomainError):
    """字段级校验失败（400）。

    code / message 由子类用 ``error_code`` / ``error_message`` 给出，details 形状
    统一为 ``{field, reason[, max_length]}``：agent 字段与 session 字段的校验只是
    错误码不同，不必各写一套 __init__。
    """

    error_code: ClassVar[str] = "INVALID_FIELD"
    error_message: ClassVar[str] = "invalid field"

    def __init__(
        self,
        *,
        field: str,
        reason: str,
        max_length: int | None = None,
    ) -> None:
        details: dict[str, Any] = {"field": field, "reason": reason}
        if max_length is not None:
            details["max_length"] = max_length
        super().__init__(
            code=type(self).error_code,
            message=type(self).error_message,
            status_code=400,
            details=details,
        )


class InvalidAgentConfigError(FieldValidationError):
    """agent 配置不合法（400）。"""

    error_code = "INVALID_AGENT_CONFIG"
    error_message = "invalid agent config"


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


class AgentNotFoundError(AgentServiceError):
    def __init__(self, *, agent_id: str) -> None:
        super().__init__(
            code=AGENT_NOT_FOUND,
            message="Agent was not found.",
            status_code=404,
            details={"agent_id": agent_id},
        )


class SessionNotFoundError(DomainError):
    def __init__(self, *, session_id: str, agent_id: str | None = None) -> None:
        details: dict[str, Any] = {"session_id": session_id}
        if agent_id is not None:
            details["agent_id"] = agent_id
        super().__init__(
            code=SESSION_NOT_FOUND,
            message="Session was not found.",
            status_code=404,
            details=details,
        )


class InvalidSessionMetadataError(FieldValidationError):
    """会话元数据不合法（400），例如标题超长。"""

    error_code = INVALID_SESSION_METADATA
    error_message = "invalid session metadata"


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


def agent_not_found(*, agent_id: str) -> AgentNotFoundError:
    """Agent 不存在，统一 404（具名子类，上层可按类型/基类捕获）。"""
    return AgentNotFoundError(agent_id=agent_id)


def session_not_found(
    *,
    session_id: str,
    agent_id: str | None = None,
) -> SessionNotFoundError:
    """会话不存在，统一 404（具名子类，便于与 agent 缺失区分）。"""
    return SessionNotFoundError(session_id=session_id, agent_id=agent_id)
