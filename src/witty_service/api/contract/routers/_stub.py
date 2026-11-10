from __future__ import annotations

from typing import NoReturn

from witty_service.api.errors import ERROR_STATUS
from witty_service.domain.error_codes import ErrorCode
from witty_service.domain.errors import DomainError


def not_implemented() -> NoReturn:
    raise DomainError(
        code=ErrorCode.ENDPOINT_NOT_IMPLEMENTED,
        message="endpoint is not implemented in the current milestone",
        status_code=ERROR_STATUS[ErrorCode.ENDPOINT_NOT_IMPLEMENTED],
    )
