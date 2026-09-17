"""
Encryption for credentials at rest (OAuth tokens for customers' own tools).

Fernet (AES-128-CBC + HMAC-SHA256) from `cryptography`, keyed from SECRET_KEY.
A database dump on its own never yields a usable token.
"""

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken

from ..core.config import settings


class VaultError(RuntimeError):
    pass


def _fernet() -> Fernet:
    if not settings.secret_key or len(settings.secret_key) < 16:
        raise VaultError("SECRET_KEY must be set (16+ characters) to store credentials")
    key = hashlib.sha256(b"creai-vault-v1:" + settings.secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def seal(data: dict) -> bytes:
    return _fernet().encrypt(json.dumps(data).encode())


def open_(blob: bytes) -> dict:
    try:
        return json.loads(_fernet().decrypt(bytes(blob)))
    except InvalidToken:
        raise VaultError("stored credential could not be decrypted")
