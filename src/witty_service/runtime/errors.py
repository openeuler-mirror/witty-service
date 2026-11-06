from __future__ import annotations

from witty_service.domain.errors import DomainError


class RuntimeContractError(DomainError):
    """Runtime seam error with stable API mapping semantics."""


class InvalidHistoryCursorError(RuntimeContractError):
    def __init__(self, *, runtime_session_id: str) -> None:
        super().__init__(
            code="INVALID_CURSOR",
            message="history cursor is invalid",
            status_code=400,
            details={"runtime_session_id": runtime_session_id},
        )


class HistoryCursorSessionMismatchError(RuntimeContractError):
    def __init__(self, *, runtime_session_id: str) -> None:
        super().__init__(
            code="CURSOR_SESSION_MISMATCH",
            message="history cursor belongs to another runtime session",
            status_code=400,
            details={"runtime_session_id": runtime_session_id},
        )


class RuntimeUnavailableError(RuntimeContractError):
    def __init__(self, *, workspace_id: str, detail: str | None = None) -> None:
        super().__init__(
            code="RUNTIME_UNAVAILABLE",
            message=detail or "workspace runtime is unavailable",
            status_code=503,
            details={"workspace_id": workspace_id},
        )


class RuntimeLeaseConflictError(RuntimeContractError):
    def __init__(self, *, lease_id: str, reason: str) -> None:
        super().__init__(
            code="RUNTIME_LEASE_CONFLICT",
            message=reason,
            status_code=409,
            details={"lease_id": lease_id},
        )


class RuntimeSessionNotFoundError(RuntimeContractError):
    def __init__(self, *, runtime_session_id: str) -> None:
        super().__init__(
            code="RUNTIME_SESSION_NOT_FOUND",
            message="runtime session was not found",
            status_code=404,
            details={"runtime_session_id": runtime_session_id},
        )


class RuntimeMessageNotFoundError(RuntimeContractError):
    def __init__(self, *, runtime_message_id: str) -> None:
        super().__init__(
            code="RUNTIME_MESSAGE_NOT_FOUND",
            message="runtime message was not found",
            status_code=404,
            details={"runtime_message_id": runtime_message_id},
        )
