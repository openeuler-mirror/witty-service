"""文件型凭据存储的通用实现。

凭据目录 0700、文件 0600、原子写入与引用格式校验在各个使用方之间完全一致；
差异只在错误码与引用前缀，由子类通过钩子提供。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

from witty_service.domain.error_codes import ErrorCode
from witty_service.domain.errors import DomainError

logger = logging.getLogger(__name__)

CREDENTIAL_FILE_VERSION = 1

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
GROUP_OTHER_BITS = 0o077

_REF_PATTERN = re.compile(r"^[a-z]{3,8}_[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class CredentialEntry:
    ref: str
    kind: str
    values: dict[str, str]
    created_at: str
    updated_at: str
    expires_at: str | None = None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


class FileCredentialStore:
    kind_by_prefix: ClassVar[dict[str, str]] = {}
    prunable_prefix: ClassVar[str | None] = None

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        raw = str(directory).strip()
        expanded = Path(raw).expanduser()
        if not raw or not expanded.is_absolute():
            raise self._unavailable(
                path=raw,
                reason=(
                    "the credential directory must be a non-empty absolute path "
                    "(a relative path would depend on the process working directory)"
                ),
            )
        self._directory = expanded

    @property
    def directory(self) -> Path:
        return self._directory

    def ensure_ready(self) -> None:
        directory = self._directory
        if not directory.exists():
            try:
                directory.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)
                os.chmod(directory, DIRECTORY_MODE)
            except OSError as exc:
                raise self._unavailable(
                    path=str(directory),
                    reason=f"cannot create the credential directory: {exc}",
                ) from exc
        self._assert_owner_only(directory, expected=DIRECTORY_MODE, kind="directory")

    def read(self, ref: str) -> CredentialEntry | None:
        path = self._path_for(ref)
        if not path.exists():
            return None
        self._assert_owner_only(path, expected=FILE_MODE, kind="file")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise self._unavailable(
                path=str(path), reason=f"cannot read the credential file: {exc}"
            ) from exc
        try:
            payload: Any = json.loads(raw)
        except ValueError as exc:
            raise self._corrupt(
                ref=ref, path=path, reason=f"not valid JSON: {exc}"
            ) from exc
        return self._decode(ref, path, payload)

    def resolve(self, ref: str) -> dict[str, str] | None:
        entry = self.read(ref)
        return None if entry is None else dict(entry.values)

    def write(
        self,
        ref: str,
        values: Mapping[str, str],
        *,
        expires_at: datetime | None = None,
    ) -> CredentialEntry:
        self.ensure_ready()
        path = self._path_for(ref)
        existing = self.read(ref)
        now = _utcnow_iso()
        payload = {
            "version": CREDENTIAL_FILE_VERSION,
            "ref": ref,
            "kind": self._kind_for(ref),
            "values": {str(key): str(value) for key, value in values.items()},
            "created_at": existing.created_at if existing is not None else now,
            "updated_at": now,
            "expires_at": (
                expires_at.astimezone(UTC).isoformat()
                if expires_at is not None
                else None
            ),
        }
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")

        tmp_path = self._directory / f".{ref}.{os.getpid()}.{uuid4().hex[:8]}.tmp"
        try:
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.chmod(tmp_path, FILE_MODE)
            os.replace(tmp_path, path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise self._unavailable(
                path=str(path), reason=f"cannot write the credential file: {exc}"
            ) from exc
        return self._decode(ref, path, payload)

    def delete(self, ref: str) -> bool:
        path = self._path_for(ref)
        try:
            os.unlink(path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise self._unavailable(
                path=str(path), reason=f"cannot delete the credential file: {exc}"
            ) from exc
        return True

    def prune_expired(self, *, now: datetime | None = None) -> int:
        prefix = self.prunable_prefix
        if prefix is None:
            return 0
        moment = now or datetime.now(UTC)
        if not self._directory.is_dir():
            return 0
        removed = 0
        for path in sorted(self._directory.glob(f"{prefix}_*.json")):
            ref = path.stem
            try:
                entry = self.read(ref)
            except DomainError as exc:
                logger.warning(
                    "Skipping an unreadable credential file %s (%s)", path, exc.code
                )
                continue
            if entry is None or entry.expires_at is None:
                continue
            try:
                expires_at = datetime.fromisoformat(entry.expires_at)
            except ValueError:
                logger.warning(
                    "Credential file %s has an unparsable expiry; skipping", path
                )
                continue
            if expires_at <= moment:
                self.delete(ref)
                removed += 1
        return removed

    def _path_for(self, ref: str) -> Path:
        prefix = ref.split("_", 1)[0] if isinstance(ref, str) else ""
        if (
            not isinstance(ref, str)
            or not _REF_PATTERN.match(ref)
            or prefix not in self.kind_by_prefix
        ):
            raise self._invalid_ref(ref=ref, reason="invalid credential reference")
        return self._directory / f"{ref}.json"

    def _kind_for(self, ref: str) -> str:
        return self.kind_by_prefix[ref.split("_", 1)[0]]

    def _assert_owner_only(self, path: Path, *, expected: int, kind: str) -> None:
        if os.name != "posix":  # pragma: no cover - 目标平台是 Linux
            return
        try:
            info = path.stat()
        except OSError as exc:
            raise self._unavailable(
                path=str(path), reason=f"cannot stat the credential {kind}: {exc}"
            ) from exc
        mode = stat.S_IMODE(info.st_mode)
        if mode & GROUP_OTHER_BITS:
            raise self._insecure(
                path=str(path),
                kind=kind,
                reason=(
                    f"mode is {mode:04o}, which is readable beyond the owner; "
                    f"run: chmod {expected:04o} {path}"
                ),
            )
        if info.st_uid != os.geteuid():
            raise self._insecure(
                path=str(path),
                kind=kind,
                reason=(
                    f"it is owned by uid {info.st_uid}, not by the running user "
                    f"(uid {os.geteuid()})"
                ),
            )

    def _decode(self, ref: str, path: Path, payload: Any) -> CredentialEntry:
        if not isinstance(payload, dict):
            raise self._corrupt(
                ref=ref, path=path, reason="the payload is not an object"
            )
        version = payload.get("version")
        if version != CREDENTIAL_FILE_VERSION:
            raise self._corrupt(
                ref=ref,
                path=path,
                reason=(
                    f"unsupported version {version!r} "
                    f"(expected {CREDENTIAL_FILE_VERSION})"
                ),
            )
        values = payload.get("values")
        if not isinstance(values, dict):
            raise self._corrupt(
                ref=ref, path=path, reason="the 'values' field is not an object"
            )
        return CredentialEntry(
            ref=ref,
            kind=str(payload.get("kind") or self._kind_for(ref)),
            values={str(key): str(value) for key, value in values.items()},
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            expires_at=(
                None
                if payload.get("expires_at") is None
                else str(payload["expires_at"])
            ),
        )

    def _unavailable(self, *, path: str, reason: str) -> DomainError:
        raise NotImplementedError

    def _invalid_ref(self, *, ref: str, reason: str) -> DomainError:
        raise NotImplementedError

    def _insecure(self, *, path: str, kind: str, reason: str) -> DomainError:
        raise NotImplementedError

    def _corrupt(self, *, ref: str, path: Path, reason: str) -> DomainError:
        raise NotImplementedError


MODEL_REF_PREFIX = "mdl"


class ModelCredentialStore(FileCredentialStore):
    kind_by_prefix: ClassVar[dict[str, str]] = {MODEL_REF_PREFIX: "model"}

    @staticmethod
    def ref_for_provider(provider_id: str, revision: int) -> str:
        return f"{MODEL_REF_PREFIX}_{_digest(f'{provider_id}:{revision}')}"

    def resolve_api_key(self, ref: str) -> str | None:
        entry = self.read(ref)
        return None if entry is None else entry.values.get("api_key")

    def _unavailable(self, *, path: str, reason: str) -> DomainError:
        return DomainError(
            code=ErrorCode.MODEL_CREDENTIAL_UNAVAILABLE,
            message="Model credential store is not usable.",
            status_code=503,
            details={"path": path, "reason": reason},
        )

    def _invalid_ref(self, *, ref: str, reason: str) -> DomainError:
        return DomainError(
            code=ErrorCode.MODEL_CREDENTIAL_INVALID,
            message="Model credential reference is invalid.",
            status_code=503,
            details={"ref": ref, "reason": reason},
        )

    def _insecure(self, *, path: str, kind: str, reason: str) -> DomainError:
        return DomainError(
            code=ErrorCode.MODEL_CREDENTIAL_UNAVAILABLE,
            message="Model credential store is readable beyond its owner.",
            status_code=503,
            details={"path": path, "kind": kind, "reason": reason},
        )

    def _corrupt(self, *, ref: str, path: Path, reason: str) -> DomainError:
        return DomainError(
            code=ErrorCode.MODEL_CREDENTIAL_INVALID,
            message="Model credential is corrupted.",
            status_code=503,
            details={"ref": ref, "path": str(path), "reason": reason},
        )
