"""Error definitions for Agent Middleware Service.

This module defines error codes and exception classes for the Agent API.
Error codes are defined in Section 8.6 of the design specification.

Error Response Structure:
{
    "error": {
        "code": "ERROR_CODE",
        "message": "Human readable message",
        "details": {}
    }
}
"""

from typing import Any, Optional


class AgentError(Exception):
    """Base exception for Agent service errors."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Optional[dict[str, Any]] = None,
        status_code: int = 500
    ):
        """Initialize error.

        Args:
            code: Error code string (e.g., "AGENT_NOT_FOUND")
            message: Human readable error message
            details: Additional error details
            status_code: HTTP status code
        """
        self.code = code
        self.message = message
        self.details = details or {}
        self.status_code = status_code
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        """Convert error to dictionary for JSON response."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


class AgentNotFoundError(AgentError):
    """Agent not found error (404)."""

    def __init__(self, agent_id: str):
        super().__init__(
            code="AGENT_NOT_FOUND",
            message=f"Agent not found: {agent_id}",
            status_code=404,
        )


class SessionNotFoundError(AgentError):
    """Session not found error (404)."""

    def __init__(self, session_id: str):
        super().__init__(
            code="SESSION_NOT_FOUND",
            message=f"Session not found: {session_id}",
            status_code=404,
        )


class SandboxNotFoundError(AgentError):
    """Sandbox not found error (404)."""

    def __init__(self, sandbox_id: str):
        super().__init__(
            code="SANDBOX_NOT_FOUND",
            message=f"Sandbox not found: {sandbox_id}",
            status_code=404,
        )


class AgentNotRunningError(AgentError):
    """Agent not running error (400)."""

    def __init__(self, agent_id: str, current_status: str):
        super().__init__(
            code="AGENT_NOT_RUNNING",
            message=f"Agent {agent_id} is not running (status: {current_status})",
            status_code=400,
            details={"current_status": current_status},
        )


class AgentPausedError(AgentError):
    """Agent paused error (400)."""

    def __init__(self, agent_id: str):
        super().__init__(
            code="AGENT_PAUSED",
            message=f"Agent {agent_id} is paused. Resume first.",
            status_code=400,
        )


class SessionNotActiveError(AgentError):
    """Session not active error (400)."""

    def __init__(self, session_id: str, current_status: str):
        super().__init__(
            code="SESSION_NOT_ACTIVE",
            message=f"Session {session_id} is not active (status: {current_status})",
            status_code=400,
            details={"current_status": current_status},
        )


class SandboxError(AgentError):
    """Sandbox operation error (500)."""

    def __init__(self, message: str, sandbox_id: Optional[str] = None):
        details = {"sandbox_id": sandbox_id} if sandbox_id else {}
        super().__init__(
            code="SANDBOX_ERROR",
            message=f"Sandbox operation failed: {message}",
            status_code=500,
            details=details,
        )


class AdapterError(AgentError):
    """Adapter execution error (500)."""

    def __init__(self, message: str, adapter_type: Optional[str] = None):
        details = {"adapter_type": adapter_type} if adapter_type else {}
        super().__init__(
            code="ADAPTER_ERROR",
            message=f"Adapter execution failed: {message}",
            status_code=500,
            details=details,
        )


class ValidationError(AgentError):
    """Request validation error (422)."""

    def __init__(self, message: str, field: Optional[str] = None):
        details = {"field": field} if field else {}
        super().__init__(
            code="VALIDATION_ERROR",
            message=f"Validation error: {message}",
            status_code=422,
            details=details,
        )


class UnauthorizedError(AgentError):
    """Unauthorized error (401)."""

    def __init__(self, message: str = "Token required"):
        super().__init__(
            code="UNAUTHORIZED",
            message=message,
            status_code=401,
        )


class ForbiddenError(AgentError):
    """Forbidden error (403)."""

    def __init__(self, message: str = "Permission denied"):
        super().__init__(
            code="FORBIDDEN",
            message=message,
            status_code=403,
        )


class InternalError(AgentError):
    """Internal server error (500)."""

    def __init__(self, message: str = "Internal server error"):
        super().__init__(
            code="INTERNAL_ERROR",
            message=message,
            status_code=500,
        )


ERROR_CODE_MAPPING = {
    "AGENT_NOT_FOUND": AgentNotFoundError,
    "SESSION_NOT_FOUND": SessionNotFoundError,
    "SANDBOX_NOT_FOUND": SandboxNotFoundError,
    "AGENT_NOT_RUNNING": AgentNotRunningError,
    "AGENT_PAUSED": AgentPausedError,
    "SESSION_NOT_ACTIVE": SessionNotActiveError,
    "SANDBOX_ERROR": SandboxError,
    "ADAPTER_ERROR": AdapterError,
    "VALIDATION_ERROR": ValidationError,
    "UNAUTHORIZED": UnauthorizedError,
    "FORBIDDEN": ForbiddenError,
    "INTERNAL_ERROR": InternalError,
}


def get_error_class(code: str) -> type[AgentError]:
    """Get error class by error code.

    Args:
        code: Error code string

    Returns:
        The corresponding error class
    """
    return ERROR_CODE_MAPPING.get(code, AgentError)