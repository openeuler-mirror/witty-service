from fastapi import Header, HTTPException, Request, status

from src.config import get_settings


def require_bearer_auth(request: Request, authorization: str | None = Header(default=None)) -> None:
    # OPTIONS 预检请求跳过认证
    if request.method == "OPTIONS":
        return

    settings = get_settings()
    expected_token = settings.auth_token

    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not expected_token or token.strip() != expected_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
