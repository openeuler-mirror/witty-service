from __future__ import annotations

from witty_service.domain.errors import DomainError


class RuntimeContractError(DomainError):
    """Runtime seam error with stable API mapping semantics."""


class RuntimeContractValidationError(RuntimeContractError):
    """seam DTO 字段不合法（400）；避免裸 ``ValueError`` 在 API 层退化为 500。"""

    def __init__(self, *, field: str, reason: str) -> None:
        super().__init__(
            code="VALIDATION_ERROR",
            message="runtime contract value is invalid",
            status_code=400,
            details={"field": field, "reason": reason},
        )


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


class RuntimeAdmissionConflictError(RuntimeContractError):
    def __init__(self, *, runtime_session_id: str, message_id: str) -> None:
        super().__init__(
            code="INVALID_TURN_OPERATION",
            message="runtime rejected a conflicting prompt admission",
            status_code=409,
            details={
                "runtime_session_id": runtime_session_id,
                "message_id": message_id,
            },
        )


class RuntimePublishUnavailableError(RuntimeContractError):
    def __init__(self, *, workspace_id: str, detail: str | None = None) -> None:
        super().__init__(
            code="CONFIG_PUBLISH_UNAVAILABLE",
            message=detail or "runtime configuration publish is unavailable",
            status_code=503,
            details={"workspace_id": workspace_id},
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


class RuntimeOperationNotImplementedError(RuntimeContractError):
    def __init__(self, *, operation: str) -> None:
        super().__init__(
            code="RUNTIME_OPERATION_NOT_IMPLEMENTED",
            message="runtime operation is not implemented",
            status_code=501,
            details={"operation": operation},
        )
