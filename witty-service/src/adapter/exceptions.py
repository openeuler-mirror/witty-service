from __future__ import annotations

from typing import Any

from src.domain.errors import DomainError


class AdaptorConnectionError(DomainError):
    code = "ADAPTOR_CONNECTION_ERROR"

    def __init__(
        self,
        *,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code=self.code, message=message, details=details)


class AdaptorConnectionTimeout(DomainError):
    code = "ADAPTOR_CONNECTION_TIMEOUT"

    def __init__(
        self,
        *,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code=self.code, message=message, details=details)


class AdaptorSendFailed(DomainError):
    code = "ADAPTOR_SEND_FAILED"

    def __init__(
        self,
        *,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code=self.code, message=message, details=details)


class AdaptorReceiveError(DomainError):
    code = "ADAPTOR_RECEIVE_ERROR"

    def __init__(
        self,
        *,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code=self.code, message=message, details=details)