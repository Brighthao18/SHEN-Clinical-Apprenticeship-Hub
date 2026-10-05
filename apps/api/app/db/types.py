from __future__ import annotations

import base64
import hashlib
import hmac
import unicodedata
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator

from app.core.config import get_settings


def _fernet() -> Fernet:
    secret = get_settings().pii_encryption_secret.encode("utf-8")
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


class EncryptedText(TypeDecorator[str]):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return _fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        try:
            return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("PII ciphertext cannot be decrypted with the configured key") from exc


def normalize_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().upper()
    return "".join(normalized.split())


def pii_fingerprint(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    key = get_settings().pii_hash_secret.encode("utf-8")
    normalized = normalize_identifier(value).encode("utf-8")
    return hmac.new(key, normalized, hashlib.sha256).hexdigest()


def redacted_change(value: Any) -> Any:
    if value is None:
        return None
    return "[PRESENT]"
