from __future__ import annotations

from datetime import UTC, datetime

from pydantic import Field, SecretStr

from witty_service.api.contract.dto.common import RequestModel, UtcDatetime
from witty_service.domain.records import ModelRecord

_DISPLAY_NAME_MAX_LENGTH = 255
_PROVIDER_ID_MAX_LENGTH = 64
_PROVIDER_MODEL_ID_MAX_LENGTH = 255
_COMPATIBILITY_MAX_LENGTH = 64
_API_BASE_URL_MAX_LENGTH = 2048


class ModelResponse(RequestModel):
    id: str
    display_name: str
    provider_id: str
    provider_model_id: str
    compatibility: str
    api_base_url: str | None
    enabled: bool
    max_tokens: int
    temperature: float
    is_default: bool
    has_credential: bool
    secret_revision: int
    created_at: UtcDatetime
    updated_at: UtcDatetime

    @classmethod
    def from_record(cls, record: ModelRecord) -> ModelResponse:
        return cls(
            id=record.id,
            display_name=record.display_name,
            provider_id=record.provider_id,
            provider_model_id=record.provider_model_id,
            compatibility=record.compatibility or "openai",
            api_base_url=record.api_base_url,
            enabled=bool(record.enabled),
            max_tokens=record.max_tokens,
            temperature=record.temperature,
            is_default=bool(record.is_default),
            has_credential=bool(record.credential_ref),
            secret_revision=record.secret_revision,
            created_at=datetime.fromtimestamp(record.created_at / 1000, UTC),
            updated_at=datetime.fromtimestamp(record.updated_at / 1000, UTC),
        )


class ModelListResponse(RequestModel):
    items: list[ModelResponse]


class CreateModelRequest(RequestModel):
    display_name: str = Field(min_length=1, max_length=_DISPLAY_NAME_MAX_LENGTH)
    provider_id: str = Field(min_length=1, max_length=_PROVIDER_ID_MAX_LENGTH)
    provider_model_id: str = Field(
        min_length=1, max_length=_PROVIDER_MODEL_ID_MAX_LENGTH
    )
    compatibility: str | None = Field(
        default=None, max_length=_COMPATIBILITY_MAX_LENGTH
    )
    api_base_url: str | None = Field(default=None, max_length=_API_BASE_URL_MAX_LENGTH)
    api_key: SecretStr | None = None
    max_tokens: int = Field(default=32768, ge=1)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    is_default: bool = False


class UpdateModelRequest(RequestModel):
    display_name: str = Field(min_length=1, max_length=_DISPLAY_NAME_MAX_LENGTH)
    max_tokens: int = Field(ge=1)
    temperature: float = Field(ge=0.0, le=2.0)
    enabled: bool
    is_default: bool


class RotateModelCredentialRequest(RequestModel):
    api_key: SecretStr
