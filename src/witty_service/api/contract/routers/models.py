from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from witty_service.api.contract.dto.model import (
    CreateModelRequest,
    ModelListResponse,
    ModelResponse,
    RotateModelCredentialRequest,
    UpdateModelRequest,
)
from witty_service.api.errors import error_responses
from witty_service.application.model_catalog_service import ModelCatalogService

router = APIRouter(prefix="/models", tags=["models"])


def _service(request: Request) -> ModelCatalogService:
    return request.app.state.services.model_catalog


Service = Annotated[ModelCatalogService, Depends(_service)]


@router.get("", response_model=ModelListResponse, responses=error_responses(400))
def list_models(service: Service, include_disabled: bool = False) -> ModelListResponse:
    return ModelListResponse(
        items=[
            ModelResponse.from_record(model)
            for model in service.list_models(include_disabled=include_disabled)
        ]
    )


@router.post(
    "",
    response_model=ModelResponse,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(400, 409, 503),
)
def create_model(payload: CreateModelRequest, service: Service) -> ModelResponse:
    model = service.create_model(
        display_name=payload.display_name,
        provider_id=payload.provider_id,
        provider_model_id=payload.provider_model_id,
        compatibility=payload.compatibility,
        api_base_url=payload.api_base_url,
        api_key=payload.api_key.get_secret_value() if payload.api_key else None,
        max_tokens=payload.max_tokens,
        temperature=payload.temperature,
        is_default=payload.is_default,
    )
    return ModelResponse.from_record(model)


@router.put(
    "/{model_id}",
    response_model=ModelResponse,
    responses=error_responses(400, 404, 409),
)
def update_model(
    model_id: str, payload: UpdateModelRequest, service: Service
) -> ModelResponse:
    model = service.update_model(
        model_id,
        display_name=payload.display_name,
        max_tokens=payload.max_tokens,
        temperature=payload.temperature,
        enabled=payload.enabled,
        is_default=payload.is_default,
    )
    return ModelResponse.from_record(model)


@router.put(
    "/{model_id}/credential",
    response_model=ModelResponse,
    responses=error_responses(400, 404, 409, 503),
)
def rotate_model_credential(
    model_id: str, payload: RotateModelCredentialRequest, service: Service
) -> ModelResponse:
    model = service.rotate_credential(
        model_id, api_key=payload.api_key.get_secret_value()
    )
    return ModelResponse.from_record(model)


@router.delete(
    "/{model_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(404, 409),
)
def delete_model(model_id: str, service: Service) -> Response:
    service.delete_model(model_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
