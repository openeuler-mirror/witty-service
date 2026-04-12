from __future__ import annotations

from copy import deepcopy
from typing import Any

from src.domain.models import ErrorPayload


class DomainError(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = deepcopy(details or {})

    def to_payload(self) -> ErrorPayload:
        return ErrorPayload(
            code=self.code,
            message=self.message,
            details=deepcopy(self.details),
        )

    def __str__(self) -> str:
        return self.message
