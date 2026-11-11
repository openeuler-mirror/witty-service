"""渠道凭据存储：在通用文件凭据存储上绑定渠道错误码与引用前缀。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from witty_service.channels import errors as err
from witty_service.domain.errors import DomainError
from witty_service.storage.credentials import (
    CREDENTIAL_FILE_VERSION,
    DIRECTORY_MODE,
    FILE_MODE,
    CredentialEntry,
    FileCredentialStore,
    _digest,
)

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期
    from witty_service.config import ChannelSettings

INSTANCE_REF_PREFIX = "chan"
PROVISIONING_REF_PREFIX = "prov"


class ChannelCredentialStore(FileCredentialStore):
    kind_by_prefix: ClassVar[dict[str, str]] = {
        INSTANCE_REF_PREFIX: "instance",
        PROVISIONING_REF_PREFIX: "provisioning",
    }

    @classmethod
    def from_settings(cls, settings: ChannelSettings) -> ChannelCredentialStore:
        return cls(settings.credentials_dir)

    @staticmethod
    def ref_for_instance(instance_id: str) -> str:
        return f"{INSTANCE_REF_PREFIX}_{_digest(instance_id)}"

    @staticmethod
    def ref_for_provisioning(attempt_id: str) -> str:
        return f"{PROVISIONING_REF_PREFIX}_{_digest(attempt_id)}"

    def prune_expired(self, *, now: datetime | None = None) -> int:
        return super().prune_expired(prefix=PROVISIONING_REF_PREFIX, now=now)

    def _unavailable(self, *, path: str, reason: str) -> DomainError:
        return err.channel_credential_store_unavailable(path=path, reason=reason)

    def _invalid_ref(self, *, ref: str, reason: str) -> DomainError:
        return err.channel_credentials_invalid(
            channel="", reason=f"invalid credential reference: {ref!r}"
        )

    def _insecure(self, *, path: str, kind: str, reason: str) -> DomainError:
        return err.channel_credential_store_insecure(
            path=path, kind=kind, reason=reason
        )

    def _corrupt(self, *, ref: str, path: Path, reason: str) -> DomainError:
        return err.channel_credentials_invalid(
            channel="", reason=f"credential {ref} at {path} is corrupted: {reason}"
        )


__all__ = [
    "CREDENTIAL_FILE_VERSION",
    "DIRECTORY_MODE",
    "FILE_MODE",
    "INSTANCE_REF_PREFIX",
    "PROVISIONING_REF_PREFIX",
    "ChannelCredentialStore",
    "CredentialEntry",
]
