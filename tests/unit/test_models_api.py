from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from witty_service.api import models as models_api
from witty_service.api.schemas import CreateModelRequest, UpdateModelRequest
from witty_service.domain.errors import DomainError
from witty_service.persistence.repositories import ModelRecord


def _model_record(**overrides: object) -> ModelRecord:
    now = datetime.now(UTC)
    data = {
        "id": "model-1",
        "name": "GPT",
        "provider": "openai",
        "compatibility": None,
        "api_key": "secret",
        "api_base_url": "https://api.openai.com/v1",
        "enabled": True,
        "max_tokens": 4096,
        "temperature": 0.7,
        "is_default": False,
        "created_at": now,
        "updated_at": now,
    }
    data.update(overrides)
    return ModelRecord(**data)


def _services() -> MagicMock:
    services = MagicMock()
    services.repository = MagicMock()
    return services


def test_create_model_uses_provider_default_api_base_url() -> None:
    services = _services()
    services.repository.create_model.return_value = _model_record()

    resp = models_api.create_model(
        payload=CreateModelRequest(
            name="GPT",
            provider="openai",
            api_key="secret",
        ),
        services=services,
    )

    services.repository.create_model.assert_called_once_with(
        name="GPT",
        provider="openai",
        api_key="secret",
        api_base_url="https://api.openai.com/v1",
        compatibility=None,
        enabled=True,
        max_tokens=4096,
        temperature=0.7,
        is_default=False,
    )
    assert resp.id == "model-1"
    assert resp.api_base_url == "https://api.openai.com/v1"


def test_list_models_returns_model_responses() -> None:
    services = _services()
    services.repository.list_models.return_value = [_model_record(name="GPT 4")]

    resp = models_api.list_models(services=services)

    assert len(resp) == 1
    assert resp[0].name == "GPT 4"


def test_delete_model_raises_domain_error_when_missing() -> None:
    services = _services()
    services.repository.get_model.return_value = None

    with pytest.raises(DomainError) as exc_info:
        models_api.delete_model("missing", services=services)

    assert exc_info.value.code == models_api.MODEL_NOT_FOUND
    assert exc_info.value.details == {"model_id": "missing"}


def test_delete_model_removes_existing_model() -> None:
    services = _services()
    services.repository.get_model.return_value = _model_record()
    backport_service = MagicMock()

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            models_api,
            "BackportService",
            lambda _services: backport_service,
        )
        resp = models_api.delete_model("model-1", services=services)

    assert resp.status_code == 204
    backport_service.clear_model_reference.assert_called_once_with("model-1")
    services.repository.delete_model.assert_called_once_with("model-1")


def test_delete_model_does_not_delete_when_backport_cleanup_fails() -> None:
    services = _services()
    services.repository.get_model.return_value = _model_record()
    backport_service = MagicMock()
    backport_service.clear_model_reference.side_effect = OSError("config is read-only")

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            models_api,
            "BackportService",
            lambda _services: backport_service,
        )
        with pytest.raises(OSError, match="config is read-only"):
            models_api.delete_model("model-1", services=services)

    services.repository.delete_model.assert_not_called()


def test_update_model_uses_provider_default_and_returns_response() -> None:
    services = _services()
    services.repository.get_model.return_value = _model_record()
    services.repository.update_model.return_value = _model_record(
        provider="anthropic",
        api_base_url="https://api.anthropic.com/v1",
        enabled=False,
    )

    resp = models_api.update_model(
        "model-1",
        payload=UpdateModelRequest(provider="anthropic", enabled=False),
        services=services,
    )

    services.repository.update_model.assert_called_once_with(
        model_id="model-1",
        name=None,
        provider="anthropic",
        api_key=None,
        api_base_url="https://api.anthropic.com/v1",
        compatibility=None,
        enabled=False,
        max_tokens=None,
        temperature=None,
        is_default=None,
    )
    assert resp.provider == "anthropic"
    assert resp.enabled is False


def test_update_model_raises_when_missing() -> None:
    services = _services()
    services.repository.get_model.return_value = None

    with pytest.raises(DomainError) as exc_info:
        models_api.update_model(
            "missing",
            payload=UpdateModelRequest(name="new-name"),
            services=services,
        )

    assert exc_info.value.code == models_api.MODEL_NOT_FOUND
