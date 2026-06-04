from __future__ import annotations

from typing import Any


class AgentSkillServiceError(Exception):
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


class RuntimeSkillsNotSupportedError(AgentSkillServiceError):
    def __init__(self, *, runtime_type: str) -> None:
        super().__init__(
            code="RUNTIME_SKILLS_NOT_SUPPORTED",
            message="runtime skills query is not supported",
            status_code=501,
            details={"runtime_type": runtime_type},
        )


class OpenClawSkillsQueryError(AgentSkillServiceError):
    def __init__(
        self,
        *,
        runtime_type: str,
        code: str,
        message: str,
    ) -> None:
        super().__init__(
            code="OPENCLAW_SKILLS_QUERY_FAILED",
            message="openclaw skills query failed",
            status_code=502,
            details={
                "runtime_type": runtime_type,
                "gateway_error_code": code,
                "gateway_error_message": message,
            },
        )
