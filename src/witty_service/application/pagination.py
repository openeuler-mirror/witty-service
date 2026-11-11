from __future__ import annotations

import base64
import json
from typing import Any

from witty_service.domain.error_codes import ErrorCode
from witty_service.domain.errors import DomainError

_CURSOR_VERSION = 1


def encode_cursor(
    *, scope: str, scope_id: str, position: dict[str, str | int]
) -> str:
    payload = {"v": _CURSOR_VERSION, "scope": scope, "id": scope_id, **position}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(
    cursor: str,
    *,
    scope: str,
    scope_id: str,
    fields: dict[str, type],
    mismatch_code: ErrorCode = ErrorCode.INVALID_CURSOR,
) -> dict[str, Any]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _cursor_error(ErrorCode.INVALID_CURSOR) from exc
    if not isinstance(payload, dict) or payload.get("v") != _CURSOR_VERSION:
        raise _cursor_error(ErrorCode.INVALID_CURSOR)
    if payload.get("scope") != scope:
        raise _cursor_error(ErrorCode.INVALID_CURSOR)
    if payload.get("id") != scope_id:
        raise _cursor_error(mismatch_code)
    position: dict[str, Any] = {}
    for name, expected in fields.items():
        value = payload.get(name)
        if not isinstance(value, expected) or isinstance(value, bool):
            raise _cursor_error(mismatch_code)
        position[name] = value
    return position


def _cursor_error(code: ErrorCode) -> DomainError:
    if code == ErrorCode.CURSOR_SESSION_MISMATCH:
        return DomainError(
            code=code,
            message="cursor does not belong to this session",
            status_code=400,
        )
    return DomainError(
        code=ErrorCode.INVALID_CURSOR,
        message="cursor is invalid",
        status_code=400,
    )
