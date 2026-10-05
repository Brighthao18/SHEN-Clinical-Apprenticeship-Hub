from __future__ import annotations

import secrets

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_mineru_provider_requires_explicit_identifiable_upload_switch() -> None:
    with pytest.raises(ValidationError, match="ALLOW_IDENTIFIABLE_MINERU_UPLOAD"):
        Settings(
            _env_file=None,
            ocr_provider="mineru",
            mineru_api_token="test-token",
            allow_identifiable_mineru_upload=False,
        )

    settings = Settings(
        _env_file=None,
        ocr_provider="mineru",
        mineru_api_token="test-token",
        allow_identifiable_mineru_upload=True,
    )
    assert settings.allow_identifiable_mineru_upload is True


def test_mineru_token_may_come_from_settings_ui_slot() -> None:
    # The token resolves at runtime (environment bootstrap first, then the
    # ``mineru_ocr`` UI slot), so startup no longer requires MINERU_API_TOKEN;
    # a missing token fails closed at extraction time instead.
    settings = Settings(
        _env_file=None,
        ocr_provider="mineru",
        allow_identifiable_mineru_upload=True,
    )
    assert settings.mineru_api_token is None


def test_production_rejects_development_secrets_and_accepts_explicit_secure_shape() -> None:
    with pytest.raises(ValidationError, match="APP_ADMIN_PASSWORD_HASH"):
        Settings(_env_file=None, app_env="production")

    with pytest.raises(ValidationError, match="high-entropy"):
        Settings(
            _env_file=None,
            app_env="production",
            database_url="postgresql+psycopg://shen:production-db-password@db:5432/shen",
            app_admin_password="",
            app_admin_password_hash="pbkdf2_sha256$600000$salt$digest",
            jwt_secret="a" * 48,
            pii_encryption_secret="b" * 48,
            pii_hash_secret="c" * 48,
        )

    settings = Settings(
        _env_file=None,
        app_env="production",
        database_url="postgresql+psycopg://shen:production-db-password@db:5432/shen",
        app_admin_password="",
        app_admin_password_hash="pbkdf2_sha256$600000$salt$digest",
        jwt_secret=secrets.token_urlsafe(48),
        pii_encryption_secret=secrets.token_urlsafe(48),
        pii_hash_secret=secrets.token_urlsafe(48),
    )
    assert settings.is_production is True
