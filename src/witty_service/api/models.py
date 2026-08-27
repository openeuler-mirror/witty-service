from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from witty_service.api.auth import require_bearer_auth
from witty_service.api.schemas import (
    CreateModelRequest,
    ModelResponse,
    UpdateModelRequest,
)
from witty_service.api.services import ServiceContainer
from witty_service.application.backport_service import BackportService
from witty_service.domain.errors import DomainError
from witty_service.persistence.repositories import ModelRecord

router = APIRouter(
    prefix="/models", tags=["models"], dependencies=[Depends(require_bearer_auth)]
)

MODEL_NOT_FOUND = "MODEL_NOT_FOUND"


def get_services(request: Request) -> ServiceContainer:
    return request.app.state.services


DEFAULT_API_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "ollama": "http://localhost:11434/v1",
    "azure": "https://{resource}.openai.azure.com",
    "deepseek": "https://api.deepseek.com/v1",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "minimax": "https://api.minimax.com/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "custom": "",  # 用户自定义，需通过 api_base_url 指定
}


@router.post("", response_model=ModelResponse, status_code=status.HTTP_201_CREATED)
def create_model(
    payload: CreateModelRequest,
    services: ServiceContainer = Depends(get_services),
) -> ModelResponse:
    api_base_url = payload.api_base_url
    if api_base_url is None:
        api_base_url = DEFAULT_API_BASE_URLS.get(payload.provider)

    model = services.repository.create_model(
        name=payload.name,
        provider=payload.provider,
        api_key=payload.api_key,
        api_base_url=api_base_url,
        compatibility=payload.compatibility,
        enabled=payload.enabled,
        max_tokens=payload.max_tokens,
        temperature=payload.temperature,
        is_default=payload.is_default,
    )
    return _to_model_response(model)


@router.get("", response_model=list[ModelResponse])
def list_models(
    services: ServiceContainer = Depends(get_services),
) -> list[ModelResponse]:
    models = services.repository.list_models()
    return [_to_model_response(model) for model in models]


@router.delete("/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_model(
    model_id: str,
    services: ServiceContainer = Depends(get_services),
) -> Response:
    model = services.repository.get_model(model_id)
    if model is None:
        raise DomainError(
            code=MODEL_NOT_FOUND,
            message="Model was not found.",
            details={"model_id": model_id},
        )
    BackportService(services).clear_model_reference(model_id)
    services.repository.delete_model(model_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{model_id}", response_model=ModelResponse)
def update_model(
    model_id: str,
    payload: UpdateModelRequest,
    services: ServiceContainer = Depends(get_services),
) -> ModelResponse:
    model = services.repository.get_model(model_id)
    if model is None:
        raise DomainError(
            code=MODEL_NOT_FOUND,
            message="Model was not found.",
            details={"model_id": model_id},
        )
    api_base_url = payload.api_base_url
    if api_base_url is None and payload.provider is not None:
        api_base_url = DEFAULT_API_BASE_URLS.get(payload.provider)
    updated_model = services.repository.update_model(
        model_id=model_id,
        name=payload.name,
        provider=payload.provider,
        api_key=payload.api_key,
        api_base_url=api_base_url,
        compatibility=payload.compatibility,
        enabled=payload.enabled,
        max_tokens=payload.max_tokens,
        temperature=payload.temperature,
        is_default=payload.is_default,
    )
    return _to_model_response(updated_model)


def _to_model_response(model: ModelRecord) -> ModelResponse:
    return ModelResponse(
        id=model.id,
        name=model.name,
        provider=model.provider,
        api_base_url=model.api_base_url,
        compatibility=model.compatibility,
        enabled=model.enabled,
        max_tokens=model.max_tokens,
        temperature=model.temperature,
        is_default=model.is_default,
        created_at=model.created_at,
        updated_at=model.updated_at,
    )
