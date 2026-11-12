from __future__ import annotations

import pytest

from witty_service.domain.error_codes import ErrorCode
from witty_service.domain.errors import DomainError
from witty_service.storage.credentials import ModelCredentialStore


@pytest.fixture()
def store(tmp_path) -> ModelCredentialStore:
    return ModelCredentialStore(tmp_path / "model-credentials")


def test_ref_is_stable_and_namespaced(store: ModelCredentialStore) -> None:
    ref = store.ref_for_provider("anthropic", 1)

    assert ref.startswith("mdl_")
    assert ref == store.ref_for_provider("anthropic", 1)
    assert ref != store.ref_for_provider("anthropic", 2)


def test_api_key_round_trip_is_owner_only(store: ModelCredentialStore) -> None:
    ref = store.ref_for_provider("openai", 1)

    store.write(ref, {"api_key": "sk-test"})

    assert store.resolve_api_key(ref) == "sk-test"
    assert (store.directory.stat().st_mode & 0o777) == 0o700
    assert ((store.directory / f"{ref}.json").stat().st_mode & 0o777) == 0o600


def test_missing_api_key_is_none(store: ModelCredentialStore) -> None:
    assert store.resolve_api_key(store.ref_for_provider("openai", 1)) is None


def test_invalid_reference_uses_model_error(store: ModelCredentialStore) -> None:
    with pytest.raises(DomainError) as excinfo:
        store.resolve_api_key("bogus")

    assert excinfo.value.code == ErrorCode.MODEL_CREDENTIAL_INVALID


def test_corrupt_file_uses_model_error(store: ModelCredentialStore) -> None:
    ref = store.ref_for_provider("openai", 1)
    store.ensure_ready()
    path = store.directory / f"{ref}.json"
    path.write_text("{broken", encoding="utf-8")
    path.chmod(0o600)

    with pytest.raises(DomainError) as excinfo:
        store.resolve_api_key(ref)

    assert excinfo.value.code == ErrorCode.MODEL_CREDENTIAL_INVALID


def test_insecure_file_uses_model_error(store: ModelCredentialStore) -> None:
    ref = store.ref_for_provider("openai", 1)
    store.write(ref, {"api_key": "sk-test"})
    (store.directory / f"{ref}.json").chmod(0o644)

    with pytest.raises(DomainError) as excinfo:
        store.resolve_api_key(ref)

    assert excinfo.value.code == ErrorCode.MODEL_CREDENTIAL_UNAVAILABLE
