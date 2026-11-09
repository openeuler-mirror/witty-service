from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from witty_service.config import get_settings
from witty_service.domain.error_codes import ErrorCode
from witty_service.domain.errors import DomainError

_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="bearerAuth",
    description="本机服务的 AUTH_TOKEN",
)


def require_bearer_auth(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    # OPTIONS 预检请求跳过认证
    if request.method == "OPTIONS":
        return

    expected_token = get_settings().auth_token
    if (
        not expected_token
        or credentials is None
        or credentials.scheme.lower() != "bearer"
        or credentials.credentials.strip() != expected_token
    ):
        raise DomainError(
            code=ErrorCode.UNAUTHORIZED,
            message="missing or invalid bearer token",
            status_code=401,
        )
