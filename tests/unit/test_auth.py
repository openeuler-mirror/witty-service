from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.security import HTTPAuthorizationCredentials

from witty_service.api import auth
from witty_service.domain.errors import DomainError


class RequestStub:
    def __init__(self, method: str) -> None:
        self.method = method


def _credentials(scheme: str, token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme=scheme, credentials=token)


def test_require_bearer_auth_skips_options() -> None:
    auth.require_bearer_auth(RequestStub("OPTIONS"), credentials=None)


@pytest.mark.parametrize(
    "credentials",
    [
        None,
        _credentials("Basic", "expected-token"),
        _credentials("Bearer", "wrong-token"),
    ],
)
def test_require_bearer_auth_rejects_invalid_token(
    monkeypatch,
    credentials: HTTPAuthorizationCredentials | None,
) -> None:
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(auth_token="expected-token"),
    )

    with pytest.raises(DomainError) as exc_info:
        auth.require_bearer_auth(RequestStub("GET"), credentials=credentials)

    assert exc_info.value.status_code == 401
    assert exc_info.value.code == "UNAUTHORIZED"


def test_require_bearer_auth_accepts_expected_token(monkeypatch) -> None:
    monkeypatch.setattr(
        auth,
        "get_settings",
        lambda: SimpleNamespace(auth_token="expected-token"),
    )

    auth.require_bearer_auth(
        RequestStub("GET"),
        credentials=_credentials("Bearer", "expected-token"),
    )
