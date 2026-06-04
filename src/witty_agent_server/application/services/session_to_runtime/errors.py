from __future__ import annotations

from typing import Any


class SessionServiceError(Exception):
    """Session 服务统一错误。"""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details


class InvalidSessionConfigError(SessionServiceError):
    def __init__(self, message: str = "invalid session config") -> None:
        super().__init__(
            code="INVALID_SESSION_CONFIG",
            message=message,
            status_code=400,
        )


class SessionNotFoundServiceError(SessionServiceError):
    def __init__(self, message: str = "session not found") -> None:
        super().__init__(
            code="SESSION_NOT_FOUND",
            message=message,
            status_code=404,
        )


class InvalidPaginationError(SessionServiceError):
    def __init__(self, *, offset: int, limit: int) -> None:
        super().__init__(
            code="INVALID_PAGINATION",
            message="invalid pagination params",
            status_code=400,
            details={"offset": offset, "limit": limit},
        )


class RuntimeNotSupportedError(SessionServiceError):
    def __init__(self, message: str = "runtime unavailable") -> None:
        super().__init__(
            code="RUNTIME_NOT_SUPPORTED_IN_IMAGE",
            message=message,
            status_code=400,
        )


class RuntimeSessionCreateFailedError(SessionServiceError):
    def __init__(self, message: str = "runtime session create failed") -> None:
        super().__init__(
            code="RUNTIME_SESSION_CREATE_FAILED",
            message=message,
            status_code=502,
        )


class RuntimeSessionDeleteFailedError(SessionServiceError):
    def __init__(self, message: str = "runtime session delete failed") -> None:
        super().__init__(
            code="RUNTIME_SESSION_DELETE_FAILED",
            message=message,
            status_code=502,
        )


class RuntimeSessionAbortFailedError(SessionServiceError):
    def __init__(self, message: str = "runtime session abort failed") -> None:
        super().__init__(
            code="RUNTIME_SESSION_ABORT_FAILED",
            message=message,
            status_code=502,
        )
