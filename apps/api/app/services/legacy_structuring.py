"""LLM-assisted structuring for legacy prescriptions (Phase 5.2).

The legacy historical-import track may use an LLM to structure OCR text that
the deterministic legacy parser cannot confidently consume. The LLM never
writes to the database directly:

1. output must satisfy the strict schema below (no extra fields, bounded
   values, per-field confidence);
2. structuring confidence must clear a configured floor, and confidence is
   never the sole auto-accept basis — the result re-enters the same drug
   normalization, dose, patient, and completeness validation gates;
3. every accepted item carries ``ai_normalized`` provenance instead of
   ``physician_record``;
4. the provider stays disabled by default until OD-010 selects a real
   provider under an approved task/data-processing boundary.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.config import get_settings

STRUCTURING_SOURCE_LLM = "llm_assisted"
STRUCTURING_SOURCE_DETERMINISTIC = "deterministic_parser"


class LegacyStructuringRejected(ValueError):
    """Raised when provider output fails the strict structuring schema."""

    def __init__(
        self, message: str, *, issue_code: str = "LLM_STRUCTURING_SCHEMA_REJECTED"
    ) -> None:
        super().__init__(message)
        self.issue_code = issue_code


class LegacyStructuringItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_text: str = Field(min_length=1, max_length=200)
    raw_drug_name: str = Field(min_length=1, max_length=40)
    processing_method: str | None = Field(default=None, max_length=20)
    explicit_dose_g: Decimal | None = Field(default=None, gt=0, le=Decimal("500"))
    package_count: Decimal | None = Field(default=None, gt=0, le=Decimal("100"))
    package_unit: Literal["包", "袋"] | None = None
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("raw_drug_name")
    @classmethod
    def _require_cjk_name(cls, value: str) -> str:
        if not any("\u4e00" <= character <= "\u9fff" for character in value):
            raise ValueError("raw_drug_name must contain CJK characters")
        return value.strip()


class LegacyStructuringDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    real_name: str | None = Field(default=None, max_length=40)
    sex: Literal["male", "female", "unknown"] = "unknown"
    age: int | None = Field(default=None, ge=0, le=130)
    encounter_date: date
    medical_record_number: str | None = Field(default=None, max_length=40)
    department: str | None = Field(default=None, max_length=60)
    physician: str | None = Field(default=None, max_length=40)
    diagnoses: list[str] = Field(default_factory=list, max_length=20)
    items: list[LegacyStructuringItem] = Field(min_length=1, max_length=60)
    total_decoctions: int | None = Field(default=None, ge=1, le=999)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("medical_record_number")
    @classmethod
    def _reject_masked_mrn(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if "*" in value:
            raise ValueError("masked medical record numbers are unusable")
        return value.strip() or None


def validate_legacy_structuring(payload: Any) -> LegacyStructuringDocument:
    """Strict schema validation for provider output; rejects anything extra."""

    try:
        document = LegacyStructuringDocument.model_validate(payload)
    except ValidationError as exc:
        raise LegacyStructuringRejected(
            f"legacy structuring payload rejected: {exc.error_count()} schema errors"
        ) from exc
    floor = get_settings().legacy_llm_structuring_confidence_floor
    weak_items = [item.raw_drug_name for item in document.items if item.confidence < floor]
    if document.confidence < floor or weak_items:
        raise LegacyStructuringRejected(
            "legacy structuring confidence is below the configured floor",
            issue_code="LLM_STRUCTURING_LOW_CONFIDENCE",
        )
    return document


class LegacyStructuringProvider(Protocol):
    name: str

    def structure(self, ocr_text: str) -> dict[str, Any]:
        """Return a raw payload for ``validate_legacy_structuring``."""


class MockLegacyStructuringProvider:
    """Deterministic test double; never installed by default."""

    name = "mock"

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self._payload = payload

    def structure(self, ocr_text: str) -> dict[str, Any]:
        if self._payload is None:
            raise LegacyStructuringRejected("mock legacy structuring provider has no payload")
        return self._payload


def get_legacy_structuring_provider() -> LegacyStructuringProvider | None:
    """Resolve the configured provider only when the assist is enabled."""

    settings = get_settings()
    if not settings.legacy_llm_structuring_enabled:
        return None
    if settings.legacy_llm_structuring_provider == "mock":
        return MockLegacyStructuringProvider()
    raise LegacyStructuringRejected(
        "no approved live legacy structuring provider is configured (OD-010 open)"
    )
