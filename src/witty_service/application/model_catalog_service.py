from __future__ import annotations

import logging
import threading

from sqlalchemy.exc import IntegrityError

from witty_service.domain.error_codes import ErrorCode
from witty_service.domain.errors import DomainError
from witty_service.domain.model_providers import (
    DEFAULT_API_BASE_URLS,
    PROVIDERS_REQUIRING_KEY,
)
from witty_service.domain.records import ModelRecord
from witty_service.persistence.entities.base import utc_ms_now
from witty_service.persistence.repository.catalog import (
    CatalogRepository,
    ProviderCredentialConflict,
)
from witty_service.storage.credentials import ModelCredentialStore

logger = logging.getLogger(__name__)


def _not_found(model_id: str) -> DomainError:
    return DomainError(
        code=ErrorCode.MODEL_NOT_FOUND,
        message="Model was not found.",
        status_code=404,
        details={"model_id": model_id},
    )


def _validation(field: str, reason: str) -> DomainError:
    return DomainError(
        code=ErrorCode.VALIDATION_ERROR,
        message="Request parameters are invalid.",
        status_code=400,
        details={"field": field, "reason": reason},
    )


def _conflict(
    code: ErrorCode, message: str, details: dict[str, object] | None = None
) -> DomainError:
    return DomainError(
        code=code,
        message=message,
        status_code=409,
        details=details or {},
    )


def _effective_endpoint(model: ModelRecord) -> str:
    return model.api_base_url or DEFAULT_API_BASE_URLS.get(model.provider_id, "")


def _effective_compatibility(model: ModelRecord) -> str:
    return model.compatibility or "openai"


def _group_credential(group: list[ModelRecord]) -> tuple[str | None, int]:
    values = {(model.credential_ref, model.secret_revision) for model in group}
    if len(values) != 1:
        raise _conflict(
            ErrorCode.MODEL_CREDENTIAL_CONFLICT,
            "Credential state is inconsistent within the provider.",
            {"provider_id": group[0].provider_id},
        )
    return next(iter(values))


class ModelCatalogService:
    def __init__(
        self,
        catalog: CatalogRepository,
        credentials: ModelCredentialStore,
    ) -> None:
        self._catalog = catalog
        self._credentials = credentials
        self._mutation_lock = threading.Lock()

    def list_models(self, *, include_disabled: bool = False) -> list[ModelRecord]:
        return self._catalog.list_models(include_disabled)

    def create_model(
        self,
        *,
        display_name: str,
        provider_id: str,
        provider_model_id: str,
        compatibility: str | None = None,
        api_base_url: str | None = None,
        api_key: str | None = None,
        max_tokens: int = 32768,
        temperature: float = 0.7,
        is_default: bool = False,
    ) -> ModelRecord:
        with self._mutation_lock:
            return self._create_locked(
                display_name=display_name,
                provider_id=provider_id,
                provider_model_id=provider_model_id,
                compatibility=compatibility,
                api_base_url=api_base_url,
                api_key=api_key or None,
                max_tokens=max_tokens,
                temperature=temperature,
                is_default=is_default,
            )

    def _create_locked(
        self,
        *,
        display_name: str,
        provider_id: str,
        provider_model_id: str,
        compatibility: str | None,
        api_base_url: str | None,
        api_key: str | None,
        max_tokens: int,
        temperature: float,
        is_default: bool,
    ) -> ModelRecord:
        endpoint = (api_base_url or "").strip() or DEFAULT_API_BASE_URLS.get(
            provider_id, ""
        )
        if not endpoint:
            raise _validation(
                "api_base_url", "the provider requires an explicit endpoint"
            )
        effective_compatibility = compatibility or "openai"
        group = self._catalog.list_provider_models(provider_id)
        now = utc_ms_now()

        if group:
            for model in group:
                if (
                    _effective_endpoint(model) != endpoint
                    or _effective_compatibility(model) != effective_compatibility
                ):
                    raise _conflict(
                        ErrorCode.MODEL_PROVIDER_CONFLICT,
                        "Provider settings conflict with existing models.",
                        {"provider_id": provider_id},
                    )
            credential_ref, secret_revision = _group_credential(group)
            if credential_ref and api_key:
                raise _conflict(
                    ErrorCode.MODEL_CREDENTIAL_CONFLICT,
                    "The provider already has a credential; rotate it instead.",
                    {"provider_id": provider_id},
                )
        else:
            if provider_id in PROVIDERS_REQUIRING_KEY and not api_key:
                raise DomainError(
                    code=ErrorCode.MODEL_CREDENTIAL_REQUIRED,
                    message="The cloud provider requires an API key.",
                    status_code=400,
                    details={"provider_id": provider_id},
                )
            credential_ref = None
            secret_revision = 0

        new_file_ref: str | None = None
        created_model_id: str | None = None
        updated_group = False
        previous_ref = credential_ref
        previous_revision = secret_revision
        try:
            if api_key and not credential_ref:
                secret_revision = 1
                credential_ref = self._credentials.ref_for_provider(provider_id, 1)
                self._credentials.write(credential_ref, {"api_key": api_key})
                new_file_ref = credential_ref
                if group:
                    try:
                        self._catalog.replace_provider_credential(
                            provider_id,
                            credential_ref,
                            secret_revision,
                            now,
                            expected_count=len(group),
                        )
                    except ProviderCredentialConflict as exc:
                        raise _conflict(
                            ErrorCode.MODEL_CREDENTIAL_CONFLICT,
                            "Concurrent credential change detected.",
                            {"provider_id": provider_id},
                        ) from exc
                    updated_group = True

            model = self._catalog.create_model(
                display_name,
                provider_id,
                provider_model_id,
                effective_compatibility,
                endpoint,
                credential_ref,
                max_tokens,
                temperature,
                now,
                secret_revision=secret_revision,
            )
            created_model_id = model.id
            if is_default:
                default = self._catalog.set_default_model(model.id, now)
                if default is None:
                    raise RuntimeError("failed to set the default model")
                model = default
            return model
        except Exception:
            if created_model_id is not None:
                self._catalog.delete_model(created_model_id)
            if new_file_ref is not None:
                reverted = not updated_group
                if updated_group:
                    try:
                        self._catalog.replace_provider_credential(
                            provider_id,
                            previous_ref,
                            previous_revision,
                            utc_ms_now(),
                        )
                        reverted = True
                    except Exception:
                        logger.exception(
                            "Failed to revert credential for provider %s", provider_id
                        )
                if reverted:
                    try:
                        self._credentials.delete(new_file_ref)
                    except DomainError:
                        logger.exception(
                            "Failed to remove credential file %s", new_file_ref
                        )
            raise

    def update_model(
        self,
        model_id: str,
        *,
        display_name: str,
        max_tokens: int,
        temperature: float,
        enabled: bool,
        is_default: bool,
    ) -> ModelRecord:
        with self._mutation_lock:
            model = self._catalog.get_model(model_id)
            if model is None:
                raise _not_found(model_id)
            if not enabled and is_default:
                raise DomainError(
                    code=ErrorCode.MODEL_DISABLED,
                    message="A disabled model cannot be the default.",
                    status_code=409,
                    details={"model_id": model_id},
                )
            now = utc_ms_now()
            updated = self._catalog.update_model(
                model_id,
                display_name=display_name,
                max_tokens=max_tokens,
                temperature=temperature,
                enabled=1 if enabled else 0,
                is_default=0,
                now=now,
            )
            if updated is None:
                raise _not_found(model_id)
            if enabled and is_default:
                default = self._catalog.set_default_model(model_id, now)
                if default is None:
                    raise _not_found(model_id)
                updated = default
            return updated

    def rotate_credential(self, model_id: str, *, api_key: str | None) -> ModelRecord:
        if not api_key:
            raise _validation("api_key", "must not be empty")
        with self._mutation_lock:
            model = self._catalog.get_model(model_id)
            if model is None:
                raise _not_found(model_id)
            group = self._catalog.list_provider_models(model.provider_id)
            if not group:
                raise _not_found(model_id)
            _, current_revision = _group_credential(group)
            new_revision = current_revision + 1
            new_ref = self._credentials.ref_for_provider(
                model.provider_id, new_revision
            )
            self._credentials.write(new_ref, {"api_key": api_key})
            try:
                self._catalog.replace_provider_credential(
                    model.provider_id,
                    new_ref,
                    new_revision,
                    utc_ms_now(),
                    expected_revision=current_revision,
                    expected_count=len(group),
                )
            except ProviderCredentialConflict as exc:
                try:
                    self._credentials.delete(new_ref)
                except DomainError:
                    logger.exception(
                        "Failed to remove a new credential file for provider %s",
                        model.provider_id,
                    )
                raise _conflict(
                    ErrorCode.MODEL_CREDENTIAL_CONFLICT,
                    "Concurrent credential change detected.",
                    {"provider_id": model.provider_id},
                ) from exc
            except Exception:
                try:
                    self._credentials.delete(new_ref)
                except DomainError:
                    logger.exception(
                        "Failed to remove a new credential file for provider %s",
                        model.provider_id,
                    )
                raise

            for old_ref in {
                item.credential_ref for item in group if item.credential_ref
            }:
                try:
                    self._credentials.delete(old_ref)
                except DomainError:
                    logger.warning(
                        "Failed to remove an old credential file for provider %s",
                        model.provider_id,
                    )
            return self._catalog.get_model(model_id) or model

    def delete_model(self, model_id: str) -> None:
        with self._mutation_lock:
            model = self._catalog.get_model(model_id)
            if model is None:
                raise _not_found(model_id)
            references = self._catalog.count_model_references(model_id)
            if any(references.values()):
                raise _conflict(
                    ErrorCode.MODEL_IN_USE,
                    "The model is still referenced; disable it instead.",
                    {"model_id": model_id, "references": references},
                )
            try:
                deleted = self._catalog.delete_model(model_id)
            except IntegrityError as exc:
                raise _conflict(
                    ErrorCode.MODEL_IN_USE,
                    "The model is still referenced; disable it instead.",
                    {"model_id": model_id},
                ) from exc
            if not deleted:
                raise _not_found(model_id)
            if (
                model.credential_ref
                and self._catalog.count_models_for_provider(model.provider_id) == 0
            ):
                try:
                    self._credentials.delete(model.credential_ref)
                except DomainError:
                    logger.warning(
                        "Failed to remove a credential file for provider %s",
                        model.provider_id,
                    )
