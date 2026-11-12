from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from witty_service.application.model_catalog_service import ModelCatalogService
from witty_service.domain.error_codes import ErrorCode
from witty_service.domain.errors import DomainError
from witty_service.domain.records import ModelRecord
from witty_service.persistence.repository.catalog import ProviderCredentialConflict


def _model(**overrides) -> ModelRecord:
    values = {
        "id": "mdl_01",
        "display_name": "Model",
        "provider_id": "openai",
        "provider_model_id": "gpt-5",
        "compatibility": "openai",
        "api_base_url": "https://api.openai.com/v1",
        "credential_ref": None,
        "enabled": 1,
        "max_tokens": 32768,
        "temperature": 0.7,
        "is_default": 0,
        "secret_revision": 0,
        "created_at": 1_000,
        "updated_at": 1_000,
    }
    values.update(overrides)
    return ModelRecord(**values)


def _service(catalog: MagicMock, credentials: MagicMock) -> ModelCatalogService:
    return ModelCatalogService(catalog, credentials)


def test_create_removes_new_file_when_insert_fails() -> None:
    catalog = MagicMock()
    catalog.list_provider_models.return_value = []
    catalog.create_model.side_effect = RuntimeError("insert failed")
    credentials = MagicMock()
    service = _service(catalog, credentials)

    with pytest.raises(RuntimeError):
        service.create_model(
            display_name="Model",
            provider_id="openai",
            provider_model_id="gpt-5",
            api_key="sk-secret",
        )

    credentials.write.assert_called_once()
    credentials.delete.assert_called_once_with(credentials.write.call_args.args[0])


def test_create_reverts_group_credential_when_insert_fails() -> None:
    existing = _model()
    catalog = MagicMock()
    catalog.list_provider_models.return_value = [existing]
    catalog.replace_provider_credential.return_value = 1
    catalog.create_model.side_effect = RuntimeError("insert failed")
    credentials = MagicMock()
    service = _service(catalog, credentials)

    with pytest.raises(RuntimeError):
        service.create_model(
            display_name="Second",
            provider_id="openai",
            provider_model_id="gpt-5-mini",
            api_key="sk-secret",
        )

    assert catalog.replace_provider_credential.call_count == 2
    revert = catalog.replace_provider_credential.call_args_list[1].args
    assert revert[:3] == ("openai", None, 0)
    credentials.delete.assert_called_once()


def test_rotation_conflict_removes_new_file_and_keeps_old_credential() -> None:
    current = _model(credential_ref="mdl_old", secret_revision=1)
    catalog = MagicMock()
    catalog.get_model.return_value = current
    catalog.list_provider_models.return_value = [current]
    catalog.replace_provider_credential.side_effect = ProviderCredentialConflict(
        "openai"
    )
    credentials = MagicMock()
    service = _service(catalog, credentials)

    with pytest.raises(DomainError) as excinfo:
        service.rotate_credential(current.id, api_key="sk-new")

    assert excinfo.value.code == ErrorCode.MODEL_CREDENTIAL_CONFLICT
    credentials.delete.assert_called_once_with(credentials.write.call_args.args[0])


def test_create_rejects_inconsistent_group_credentials() -> None:
    first = _model(credential_ref="mdl_a", secret_revision=1)
    second = _model(id="mdl_02", credential_ref="mdl_b", secret_revision=1)
    catalog = MagicMock()
    catalog.list_provider_models.return_value = [first, second]
    credentials = MagicMock()
    service = _service(catalog, credentials)

    with pytest.raises(DomainError) as excinfo:
        service.create_model(
            display_name="Third",
            provider_id="openai",
            provider_model_id="gpt-5-mini",
        )

    assert excinfo.value.code == ErrorCode.MODEL_CREDENTIAL_CONFLICT
    credentials.write.assert_not_called()


def test_rotation_updates_group_and_removes_old_file() -> None:
    current = _model(credential_ref="mdl_old", secret_revision=1)
    rotated = _model(credential_ref="mdl_new", secret_revision=2)
    catalog = MagicMock()
    catalog.get_model.side_effect = [current, rotated]
    catalog.list_provider_models.return_value = [current]
    catalog.replace_provider_credential.return_value = 1
    credentials = MagicMock()
    service = _service(catalog, credentials)

    result = service.rotate_credential(current.id, api_key="sk-new")

    assert result.secret_revision == 2
    assert (
        catalog.replace_provider_credential.call_args.kwargs["expected_revision"] == 1
    )
    credentials.delete.assert_called_once_with("mdl_old")
