"""凭据加密的测试：密钥校验 fail-closed、密文可逆、损坏密文可识别。"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from witty_service.channels import errors as err
from witty_service.channels.crypto import CredentialCipher
from witty_service.domain.errors import DomainError


def _key() -> str:
    return Fernet.generate_key().decode()


def test_round_trip() -> None:
    cipher = CredentialCipher(_key())

    token = cipher.encrypt_json({"secret": "s3cr3t", "token": "t"})

    assert b"s3cr3t" not in token
    assert cipher.decrypt_json(token) == {"secret": "s3cr3t", "token": "t"}


@pytest.mark.parametrize("key", [None, "", "not-a-fernet-key", "short"])
def test_missing_or_invalid_key_is_rejected(key: str | None) -> None:
    """密钥缺失或非法时拒绝启动（fail-closed，框架设计 §6.2）。"""
    with pytest.raises(DomainError) as excinfo:
        CredentialCipher.from_settings(key)

    assert excinfo.value.code == err.CHANNEL_SECRET_KEY_INVALID


def test_ciphertext_from_another_key_is_rejected() -> None:
    token = CredentialCipher(_key()).encrypt_json({"secret": "s"})

    with pytest.raises(DomainError) as excinfo:
        CredentialCipher(_key()).decrypt_json(token)

    assert excinfo.value.code == err.CHANNEL_CREDENTIALS_INVALID


def test_plaintext_payload_is_not_decryptable() -> None:
    cipher = CredentialCipher(_key())

    with pytest.raises(DomainError):
        cipher.decrypt_json(b"plain text is not a fernet token")
