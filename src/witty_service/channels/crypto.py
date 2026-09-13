"""凭据加密：Fernet 对称加密（框架设计 §6.2）。

- 密钥来自 `WITTY_CHANNEL_SECRET_KEY`（base64 编码的 32 字节），**不落库**，由部署环境
  注入，因此进程重启后无需额外机制即可解密；
- **密钥解析与校验发生在 `ChannelGateway.start()`**（本模块的 `from_settings`），
  而不是配置读取阶段——配置层只保存原始字符串；
- 缺少 `cryptography` 或密钥非法时**拒绝启动**（fail-closed）；
- 密钥轮换不在本期范围内（未决项 U4）：当前语义是"换密钥即全部重新接入"。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from witty_service.channels import errors as err

__all__ = ["CredentialCipher"]


class CredentialCipher:
    """凭据与平台临时凭据的加解密。"""

    def __init__(self, key: str | bytes) -> None:
        raw = key.encode("utf-8") if isinstance(key, str) else key
        try:
            self._fernet = Fernet(raw)
        except Exception as exc:  # cryptography 对非法密钥抛多种异常
            raise err.channel_secret_key_invalid(
                reason=f"Fernet key is invalid: {exc}"
            ) from exc

    @classmethod
    def from_settings(cls, secret_key: str | None) -> CredentialCipher:
        """从配置构造；缺失或非法时抛 `CHANNEL_SECRET_KEY_INVALID`（fail-closed）。"""
        if not secret_key:
            raise err.channel_secret_key_invalid(
                reason="WITTY_CHANNEL_SECRET_KEY is not set"
            )
        return cls(secret_key)

    def encrypt_json(self, payload: Mapping[str, Any]) -> bytes:
        data = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
        return self._fernet.encrypt(data)

    def decrypt_json(self, token: bytes) -> dict[str, str]:
        """解密为字符串字典；密钥不匹配或内容损坏时抛 `CHANNEL_CREDENTIALS_INVALID`。"""
        try:
            raw = self._fernet.decrypt(token)
        except InvalidToken as exc:
            raise err.channel_credentials_invalid(
                channel="", reason="ciphertext cannot be decrypted with current key"
            ) from exc
        decoded = json.loads(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise err.channel_credentials_invalid(
                channel="", reason="ciphertext payload is not an object"
            )
        return {str(key): str(value) for key, value in decoded.items()}
