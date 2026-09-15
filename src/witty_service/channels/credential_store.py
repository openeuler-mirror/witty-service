"""渠道凭据存储：平台凭据放在主库之外的 0600 文件里。

**纪律（每一条都有对应的拒绝路径）**

- 凭据目录 0700，由本模块创建；已存在但 group/other 有任何权限位时**拒绝使用**；
- 一个凭据一个文件 `<ref>.json`，0600；读取前校验权限与所有者；
- 写入走"同目录临时文件（0600 建）→ `os.replace`"，不会出现半截文件，也不会
  留下权限更宽的中间产物；
- 引用（`ref`）**不是秘密**，它是实例 id 的 SHA-256 摘要，落在数据库里；
  凭据本体只存在于文件系统。

**边界说明**

这是审慎，不是边界：以同一 OS 用户身份运行的 agent 工具进程读这些文件，与读该
用户拥有的任何其他文件没有区别。要让凭据远离自身 agent，只能把它放进另一个信任
域（另一个 UID / 另一台机器）——文件权限做不到。
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
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from witty_service.channels import errors as err
from witty_service.domain.errors import DomainError

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期
    from witty_service.config import ChannelSettings

logger = logging.getLogger(__name__)

#: 凭据文件的格式版本；不认识的高版本一律拒绝加载
CREDENTIAL_FILE_VERSION = 1

#: 实例凭据的引用前缀
INSTANCE_REF_PREFIX = "chan"
#: 平台临时凭据（接入尝试状态）的引用前缀
PROVISIONING_REF_PREFIX = "prov"

#: 凭据目录权限：仅所有者可读写执行
DIRECTORY_MODE = 0o700
#: 凭据文件权限：仅所有者可读写
FILE_MODE = 0o600
#: group / other 的任何权限位
GROUP_OTHER_BITS = 0o077

#: 引用字面量：`<前缀>_<32 位十六进制摘要>`。**严格校验**，因为 ref 来自数据库，
#: 而它会被拼进文件路径（防目录穿越）。
_REF_PATTERN = re.compile(r"^[a-z]{3,8}_[0-9a-f]{32}$")

_KIND_BY_PREFIX = {
    INSTANCE_REF_PREFIX: "instance",
    PROVISIONING_REF_PREFIX: "provisioning",
}


@dataclass(frozen=True, slots=True)
class CredentialEntry:
    """一个凭据文件的只读视图。"""

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


def _kind_for(ref: str) -> str:
    return _KIND_BY_PREFIX[ref.split("_", 1)[0]]


class ChannelCredentialStore:
    """凭据文件存储：一个目录、一个凭据一个 0600 文件。"""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        raw = str(directory).strip()
        expanded = Path(raw).expanduser()
        if not raw or not expanded.is_absolute():
            # 相对路径会把"凭据放哪儿"交给进程的工作目录，而工作目录随启动方式变化
            # （systemd / 开发态 / 测试）：那意味着凭据可能被写进仓库里，或者
            # "文件不见了"变成一台机器一个样。
            raise err.channel_credential_store_unavailable(
                path=raw,
                reason=(
                    "the credential directory must be a non-empty absolute path "
                    "(a relative path would depend on the process working directory)"
                ),
            )
        self._directory = expanded

    @classmethod
    def from_settings(cls, settings: ChannelSettings) -> ChannelCredentialStore:
        return cls(settings.credentials_dir)

    # ==========================================================================
    # 引用（非密，可落库）
    # ==========================================================================

    @staticmethod
    def ref_for_instance(instance_id: str) -> str:
        """实例凭据的引用：实例 id 的摘要。**同一个实例永远是同一个引用**。"""
        return f"{INSTANCE_REF_PREFIX}_{_digest(instance_id)}"

    @staticmethod
    def ref_for_provisioning(attempt_id: str) -> str:
        """接入尝试的平台临时凭据的引用：尝试 id 的摘要。"""
        return f"{PROVISIONING_REF_PREFIX}_{_digest(attempt_id)}"

    @property
    def directory(self) -> Path:
        return self._directory

    # ==========================================================================
    # 目录
    # ==========================================================================

    def ensure_ready(self) -> None:
        """确保凭据目录存在且为 0700；否则抛域错误（网关据此拒绝启动）。"""
        directory = self._directory
        if not directory.exists():
            try:
                directory.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)
                # makedirs 的 mode 会被 umask 削，且不作用于中间目录：叶子目录显式再设一次
                os.chmod(directory, DIRECTORY_MODE)
            except OSError as exc:
                raise err.channel_credential_store_unavailable(
                    path=str(directory),
                    reason=f"cannot create the credential directory: {exc}",
                ) from exc
        self._assert_owner_only(directory, expected=DIRECTORY_MODE, kind="directory")

    # ==========================================================================
    # 读写
    # ==========================================================================

    def read(self, ref: str) -> CredentialEntry | None:
        """读取一个凭据；文件不存在返回 None，内容损坏抛 `CHANNEL_CREDENTIALS_INVALID`。"""
        path = self._path_for(ref)
        if not path.exists():
            return None
        self._assert_owner_only(path, expected=FILE_MODE, kind="file")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise err.channel_credential_store_unavailable(
                path=str(path), reason=f"cannot read the credential file: {exc}"
            ) from exc
        try:
            payload: Any = json.loads(raw)
        except ValueError as exc:
            raise self._corrupt(ref, path, f"not valid JSON: {exc}") from exc
        return self._decode(ref, path, payload)

    def resolve(self, ref: str) -> dict[str, str] | None:
        """读取一个凭据的字段字典；不存在返回 None。"""
        entry = self.read(ref)
        return None if entry is None else dict(entry.values)

    def write(
        self,
        ref: str,
        values: Mapping[str, str],
        *,
        expires_at: datetime | None = None,
    ) -> CredentialEntry:
        """原子写入一个凭据（临时文件 0600 → `os.replace`）。"""
        self.ensure_ready()
        path = self._path_for(ref)
        existing = self.read(ref)
        now = _utcnow_iso()
        payload = {
            "version": CREDENTIAL_FILE_VERSION,
            "ref": ref,
            "kind": _kind_for(ref),
            "values": {str(key): str(value) for key, value in values.items()},
            "created_at": existing.created_at if existing is not None else now,
            "updated_at": now,
            "expires_at": (
                expires_at.astimezone(UTC).isoformat() if expires_at is not None else None
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
            # O_CREAT 的 mode 会被 umask 削（只会更严），这里显式设一次保证确定性
            os.chmod(tmp_path, FILE_MODE)
            os.replace(tmp_path, path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise err.channel_credential_store_unavailable(
                path=str(path), reason=f"cannot write the credential file: {exc}"
            ) from exc
        return self._decode(ref, path, payload)

    def delete(self, ref: str) -> bool:
        """删除一个凭据；文件本来就不存在时返回 False。"""
        path = self._path_for(ref)
        try:
            os.unlink(path)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise err.channel_credential_store_unavailable(
                path=str(path), reason=f"cannot delete the credential file: {exc}"
            ) from exc
        return True

    def prune_expired(self, *, now: datetime | None = None) -> int:
        """回收已过期的平台临时凭据（接入尝试状态）。

        只扫描 `prov_` 前缀的文件；实例凭据永远不在这里被回收。无法校验的文件
        只记日志、不删除——宁可留一个可疑文件，也不要删掉一个读不懂的文件。
        """
        moment = now or datetime.now(UTC)
        if not self._directory.is_dir():
            return 0
        removed = 0
        for path in sorted(self._directory.glob(f"{PROVISIONING_REF_PREFIX}_*.json")):
            ref = path.stem
            try:
                entry = self.read(ref)
            except DomainError as exc:
                logger.warning(
                    "Skipping an unreadable provisioning credential file %s (%s)",
                    path,
                    exc.code,
                )
                continue
            if entry is None or entry.expires_at is None:
                continue
            try:
                expires_at = datetime.fromisoformat(entry.expires_at)
            except ValueError:
                logger.warning(
                    "Provisioning credential file %s has an unparsable expiry; skipping",
                    path,
                )
                continue
            if expires_at <= moment:
                self.delete(ref)
                removed += 1
        return removed

    # ==========================================================================
    # 内部
    # ==========================================================================

    def _path_for(self, ref: str) -> Path:
        if (
            not isinstance(ref, str)
            or not _REF_PATTERN.match(ref)
            or ref.split("_", 1)[0] not in _KIND_BY_PREFIX
        ):
            raise err.channel_credentials_invalid(
                channel="", reason=f"invalid credential reference: {ref!r}"
            )
        return self._directory / f"{ref}.json"

    def _assert_owner_only(self, path: Path, *, expected: int, kind: str) -> None:
        """权限与所有者校验：不满足即拒绝使用（fail-closed）。

        非 POSIX 平台没有可依赖的权限位，跳过校验（与 DSH 凭据存储的处理一致）。
        """
        if os.name != "posix":  # pragma: no cover - 目标平台是 Linux
            return
        try:
            info = path.stat()
        except OSError as exc:
            raise err.channel_credential_store_unavailable(
                path=str(path), reason=f"cannot stat the credential {kind}: {exc}"
            ) from exc
        mode = stat.S_IMODE(info.st_mode)
        if mode & GROUP_OTHER_BITS:
            raise err.channel_credential_store_insecure(
                path=str(path),
                kind=kind,
                reason=(
                    f"mode is {mode:04o}, which is readable beyond the owner; "
                    f"run: chmod {expected:04o} {path}"
                ),
            )
        if info.st_uid != os.geteuid():
            raise err.channel_credential_store_insecure(
                path=str(path),
                kind=kind,
                reason=(
                    f"it is owned by uid {info.st_uid}, not by the running user "
                    f"(uid {os.geteuid()})"
                ),
            )

    @staticmethod
    def _corrupt(ref: str, path: Path, reason: str) -> DomainError:
        return err.channel_credentials_invalid(
            channel="", reason=f"credential {ref} at {path} is corrupted: {reason}"
        )

    def _decode(self, ref: str, path: Path, payload: Any) -> CredentialEntry:
        if not isinstance(payload, dict):
            raise self._corrupt(ref, path, "the payload is not an object")
        version = payload.get("version")
        if version != CREDENTIAL_FILE_VERSION:
            raise self._corrupt(
                ref,
                path,
                f"unsupported version {version!r} (expected {CREDENTIAL_FILE_VERSION})",
            )
        values = payload.get("values")
        if not isinstance(values, dict):
            raise self._corrupt(ref, path, "the 'values' field is not an object")
        return CredentialEntry(
            ref=ref,
            kind=str(payload.get("kind") or _kind_for(ref)),
            values={str(key): str(value) for key, value in values.items()},
            created_at=str(payload.get("created_at") or ""),
            updated_at=str(payload.get("updated_at") or ""),
            expires_at=(
                None if payload.get("expires_at") is None else str(payload["expires_at"])
            ),
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
