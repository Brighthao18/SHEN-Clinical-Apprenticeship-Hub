from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = "development"
    app_version: str = "0.2.0-rc.1"
    app_timezone: str = "Asia/Singapore"
    database_url: str = "sqlite+pysqlite:///./work/shen-dev.db"
    redis_url: str = "redis://localhost:6379/0"
    tasks_eager: bool = False

    storage_backend: str = "local"
    storage_root: Path = Path("data/storage")
    max_upload_bytes: int = 25 * 1024 * 1024
    # Read-only view over the backup root written by scripts/clinical_backup.py
    # (each backup dir holds backup_manifest.json; the restore drill adds
    # restore_verification.json). Empty disables the status surface; Docker
    # deployments need the backup root mounted read-only into the API.
    backup_manifest_dir: str = ""

    app_admin_username: str = "admin"
    app_admin_password: str = "shen-dev-only"
    app_admin_password_hash: str | None = None
    jwt_secret: str = "shen-development-jwt-secret-change-before-real-data"
    jwt_expire_minutes: int = 480
    auth_disabled: bool = False

    pii_encryption_secret: str = "shen-development-encryption-secret"
    pii_hash_secret: str = "shen-development-hmac-secret"

    ocr_provider: str = "mock"
    mineru_base_url: str = "https://mineru.net"
    mineru_api_token: str | None = None
    mineru_model_version: str = "vlm"
    mineru_language: str = "ch"
    mineru_poll_interval_seconds: float = 3.0
    mineru_timeout_seconds: int = 300
    allow_identifiable_mineru_upload: bool = False

    celery_result_expires_seconds: int = Field(default=7 * 24 * 60 * 60, ge=1, le=7 * 24 * 60 * 60)

    web_origin: str = "http://localhost:3000"
    # Phase 6: AI-primary ingestion. The deterministic template parsers stay
    # as legacy/fallback and regression reference; the main chain is
    # MinerU -> ClinicalAIExtractor -> Validator -> Resolution.
    pipeline_version: str = "6.2"
    ai_extraction_enabled: bool = True
    ai_extraction_provider: str = "mock"
    ai_extraction_confidence_floor: float = Field(default=0.9, ge=0, le=1)
    ai_extractor_version: str = "0.1.0"
    ai_extraction_fixture_path: Path = (
        Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "ai_extraction_fixtures.json"
    )
    # Optional second-pass verification: only ever runs after the validator
    # raised blocking issues, and only to re-check the original OCR.
    ai_verification_enabled: bool = False
    # Phase 6.1 live provider (OpenAI-compatible structured output). The key
    # never appears in logs or CLI arguments; only the mock provider is the
    # default. Real OCR contains identifiers, so a live provider additionally
    # requires the backend-only explicit privacy switch.
    ai_provider_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    ai_provider_api_key: str | None = None
    ai_provider_model: str = "glm-4.7"
    ai_provider_timeout_seconds: float = 120.0
    ai_extraction_allow_identifiable_input: bool = False
    # Phase 6.2 Vision Verification Fallback (the final ingestion capability):
    # when the validator blocks on OCR-source incompleteness, the ORIGINAL
    # case image may be sent once to an image-capable provider for a
    # constrained per-field re-read. The image crosses a privacy boundary
    # exactly like MinerU, so the backend-only switch is mandatory, the
    # feature itself is off by default, and an unclear read must stay NULL.
    ai_vision_verification_enabled: bool = False
    allow_identifiable_ai_vision_upload: bool = False
    ai_vision_provider_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    ai_vision_provider_api_key: str | None = None
    ai_vision_provider_model: str = "glm-4.6v"
    ai_vision_provider_timeout_seconds: float = 180.0
    ai_vision_confidence_floor: float = Field(default=0.9, ge=0, le=1)
    jnu_parser_version: str = "1.1.0"
    jnu_legacy_parser_version: str = "0.2.2"
    drug_dictionary_version: str = "seed-20260828"
    legacy_llm_structuring_enabled: bool = False
    legacy_llm_structuring_provider: str = "mock"
    legacy_llm_structuring_confidence_floor: float = Field(default=0.9, ge=0, le=1)
    fuzzy_auto_match_enabled: bool = False
    fuzzy_match_threshold: float = Field(default=0.95, ge=0, le=1)
    fuzzy_match_margin: float = Field(default=0.10, ge=0, le=1)

    @model_validator(mode="after")
    def validate_provider(self) -> Settings:
        if self.ocr_provider not in {"mock", "mineru"}:
            raise ValueError("OCR_PROVIDER must be 'mock' or 'mineru'")
        if self.ai_extraction_provider not in {"mock", "live_openai_compatible"}:
            raise ValueError(
                "AI_EXTRACTION_PROVIDER must be 'mock' or 'live_openai_compatible'"
            )
        if self.ai_extraction_provider == "live_openai_compatible":
            if not self.ai_provider_api_key:
                raise ValueError(
                    "AI_PROVIDER_API_KEY is required when AI_EXTRACTION_PROVIDER="
                    "live_openai_compatible"
                )
            if not self.ai_provider_model:
                raise ValueError(
                    "AI_PROVIDER_MODEL is required when AI_EXTRACTION_PROVIDER="
                    "live_openai_compatible"
                )
            if not self.ai_extraction_allow_identifiable_input:
                raise ValueError(
                    "AI_EXTRACTION_ALLOW_IDENTIFIABLE_INPUT=true is required for a live "
                    "extraction provider: real OCR text contains patient identifiers "
                    "(explicit user-approved boundary, mirrors ALLOW_IDENTIFIABLE_MINERU_UPLOAD)"
                )
        # The MinerU token may come from the environment (bootstrap/ops) or
        # from the settings-UI slot ``mineru_ocr`` (encrypted at rest). A
        # missing token fails closed at extraction time, not at startup.
        # The backend-only identifiable-upload switch is mandatory either
        # way because MinerU receives ORIGINAL files.
        if self.ocr_provider == "mineru" and not self.allow_identifiable_mineru_upload:
            raise ValueError(
                "ALLOW_IDENTIFIABLE_MINERU_UPLOAD=true is required when OCR_PROVIDER=mineru"
            )
        # Everyday path: the vision provider is configured through the
        # settings UI (slot ``vision_verification``, key encrypted at
        # rest, explicit image-upload confirmation). The environment
        # variables are only the bootstrap path and then require the
        # same backend-only privacy switch as MinerU.
        if self.ai_vision_verification_enabled and self.ai_vision_provider_api_key:
            if not self.allow_identifiable_ai_vision_upload:
                raise ValueError(
                    "ALLOW_IDENTIFIABLE_AI_VISION_UPLOAD=true is required when "
                    "AI_VISION_PROVIDER_API_KEY is set: the original case image "
                    "contains patient identifiers (explicit user-approved "
                    "boundary, mirrors ALLOW_IDENTIFIABLE_MINERU_UPLOAD)"
                )
            if not self.ai_vision_provider_model:
                raise ValueError(
                    "AI_VISION_PROVIDER_MODEL is required when "
                    "AI_VISION_PROVIDER_API_KEY is set"
                )
        if self.is_production:
            insecure_defaults = {
                "shen-development-jwt-secret-change-before-real-data",
                "shen-development-encryption-secret",
                "shen-development-hmac-secret",
            }
            if self.auth_disabled:
                raise ValueError("AUTH_DISABLED cannot be true in production")
            if not self.app_admin_password_hash or not self.app_admin_password_hash.startswith(
                "pbkdf2_sha256$"
            ):
                raise ValueError("APP_ADMIN_PASSWORD_HASH is required in production")
            if self.app_admin_password:
                raise ValueError("APP_ADMIN_PASSWORD must be empty in production")
            for name, value in (
                ("JWT_SECRET", self.jwt_secret),
                ("PII_ENCRYPTION_SECRET", self.pii_encryption_secret),
                ("PII_HASH_SECRET", self.pii_hash_secret),
            ):
                if not _looks_high_entropy(value) or value in insecure_defaults:
                    raise ValueError(f"{name} must be a non-default high-entropy production secret")
            if self.pii_encryption_secret == self.pii_hash_secret:
                raise ValueError("PII encryption and HMAC secrets must be different")
            database = urlparse(self.database_url.replace("postgresql+psycopg", "postgresql", 1))
            if database.scheme != "postgresql" or not database.password:
                raise ValueError(
                    "Production DATABASE_URL must be password-authenticated PostgreSQL"
                )
            if "shen-dev-password" in self.database_url or "development" in self.database_url:
                raise ValueError("Development database credentials cannot be used in production")
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _looks_high_entropy(value: str) -> bool:
    return len(value) >= 43 and len(set(value)) >= 16
