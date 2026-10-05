"""ClinicalAIExtractor — the unified AI structuring stage (Phase 6).

Replaces template parsers as the primary structuring path for both the
current outpatient-record layout and legacy prescription slips. MinerU only
produces OCR text/layout; this stage is solely responsible for medical
semantic understanding, under deterministic gates:

1. Strict structured output: the provider payload must satisfy the Pydantic
   schema exactly (``extra="forbid"``); free Markdown is never stored as
   structured clinical data.
2. Missing means NULL: only facts present in the source may be extracted.
   Missing optional fields must be ``null``; no pattern inference, no
   common-sense history, no guessed grams. Medical inference belongs to the
   separate AIAnalysis layer, never to source extraction.
3. Evidence: every prescription item carries ``original_text`` that must be
   traceable to the stored OCR text (verified deterministically below).
4. Dictionary gate: the AI may *suggest* a canonical drug name, but only the
   DrugDictionary resolves identity; an unknown drug fails closed to
   UNRESOLVED and the AI can never create canonical entries.
5. Dose arithmetic stays outside the AI: only ``explicit_package_dose_g``,
   ``package_count`` and the printed unit are extracted; ``(10g) x 2 = 20g
   per_decoction`` is computed by deterministic services downstream.
6. Provenance: accepted extraction results are stored with
   ``ai_extracted`` structuring source so no AI output is ever presented as
   a physician record.

The provider is mock-only until OD-010 approves a live vendor under an
accepted data-processing boundary. Provider confidence never auto-accepts a
document by itself; every result re-enters the deterministic validator.
"""

from __future__ import annotations

import hashlib
import json
import logging
import unicodedata
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.config import get_settings
from app.models.enums import DiagnosisType, Sex, SymptomStatus, SymptomTrend
from app.services.parsers.jnu_legacy import flatten_mineru_markup
from app.services.parsers.types import (
    ParsedDiagnosis,
    ParsedDocument,
    ParsedPrescriptionItem,
    ParsedSymptom,
)

logger = logging.getLogger(__name__)

STRUCTURING_SOURCE_AI = "ai_extracted"

ISSUE_SCHEMA_REJECTED = "AI_EXTRACTION_SCHEMA_REJECTED"
ISSUE_LOW_CONFIDENCE = "AI_EXTRACTION_LOW_CONFIDENCE"
ISSUE_EVIDENCE_MISSING = "AI_EXTRACTION_EVIDENCE_MISSING"
ISSUE_PROVIDER_FAILED = "AI_EXTRACTION_PROVIDER_FAILED"

# Warning vocabulary the extractor may emit that maps onto existing blocking
# validation codes. Anything outside this list is stored for diagnostics and
# never weakens a validation decision (fail closed).
RECOGNIZED_WARNING_CODES = frozenset(
    {
        "MULTIPLE_PATIENT_NAMES_SUSPECTED",
        "UNLABELED_HEADER_MRN_INFERRED",
    }
)


class ClinicalExtractionRejected(ValueError):
    """Raised when provider output fails the strict extraction gates."""

    def __init__(self, message: str, *, issue_code: str = ISSUE_SCHEMA_REJECTED) -> None:
        super().__init__(message)
        self.issue_code = issue_code


class PatientExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    real_name: str | None = Field(default=None, min_length=1, max_length=40)
    sex: Literal["male", "female", "other", "unknown"] = "unknown"
    age: int | None = Field(default=None, ge=0, le=130)
    medical_record_number: str | None = Field(default=None, min_length=1, max_length=40)

    @field_validator("real_name", "medical_record_number")
    @classmethod
    def _strip_or_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None

    @field_validator("medical_record_number")
    @classmethod
    def _reject_masked_mrn(cls, value: str | None) -> str | None:
        if value is not None and "*" in value:
            raise ValueError("masked medical record numbers are unusable for identity")
        return value


class EncounterExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hospital: str | None = Field(default=None, min_length=1, max_length=120)
    department: str | None = Field(default=None, min_length=1, max_length=60)
    physician: str | None = Field(default=None, min_length=1, max_length=40)
    encounter_date: date | None = None
    chief_complaint: str | None = Field(default=None, min_length=1, max_length=500)
    present_illness: str | None = Field(default=None, min_length=1, max_length=2000)
    past_history: str | None = Field(default=None, min_length=1, max_length=2000)
    allergy: str | None = Field(default=None, min_length=1, max_length=500)
    vital_signs: str | None = Field(default=None, min_length=1, max_length=500)
    advice: str | None = Field(default=None, min_length=1, max_length=1000)
    treatment_record: str | None = Field(default=None, min_length=1, max_length=2000)

    @field_validator("*")
    @classmethod
    def _strip_or_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value


class SymptomExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=120)
    status: Literal["present", "absent", "unknown"] = "present"
    trend: Literal["new", "persistent", "improved", "worsened", "resolved", "unknown"] | None = (
        None
    )

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class DiagnosisExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=120)
    diagnosis_type: Literal[
        "western", "tcm_disease", "tcm_pattern", "procedure_status", "problem"
    ] = "problem"
    is_primary: bool = False

    @field_validator("text")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class PrescriptionItemExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Evidence span; must be traceable to the stored OCR text.
    original_text: str = Field(min_length=1, max_length=200)
    raw_drug_name: str = Field(min_length=1, max_length=40)
    processing_method: str | None = Field(default=None, min_length=1, max_length=20)
    # AI proposal only. Identity is resolved exclusively through the
    # DrugDictionary; an unmatched suggestion fails closed to UNRESOLVED.
    suggested_canonical_name: str | None = Field(default=None, min_length=1, max_length=40)
    is_basic_drug: bool = False
    explicit_package_dose_g: Decimal | None = Field(default=None, gt=0, le=Decimal("500"))
    package_count: Decimal | None = Field(default=None, gt=0, le=Decimal("100"))
    package_unit: Literal["包", "袋"] | None = None
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("raw_drug_name", "suggested_canonical_name", "processing_method")
    @classmethod
    def _strip_or_none(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None

    @field_validator("raw_drug_name")
    @classmethod
    def _require_cjk_name(cls, value: str) -> str:
        if not any("\u4e00" <= character <= "\u9fff" for character in value):
            raise ValueError("raw_drug_name must contain CJK characters")
        return value


class PrescriptionExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_decoctions: int | None = Field(default=None, ge=1, le=999)
    raw_text: str | None = Field(default=None, max_length=8000)
    items: list[PrescriptionItemExtraction] = Field(default_factory=list, max_length=80)


class ClinicalExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient: PatientExtraction
    encounter: EncounterExtraction
    symptoms: list[SymptomExtraction] = Field(default_factory=list, max_length=60)
    diagnoses: list[DiagnosisExtraction] = Field(default_factory=list, max_length=20)
    prescription: PrescriptionExtraction
    # Fixed vocabulary; recognized codes may only add blocking signals.
    extraction_warnings: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0.0, le=1.0)


def evidence_key(text: str) -> str:
    """Canonical comparison key for OCR evidence matching.

    NFKC unifies full/half-width punctuation, MinerU markup is flattened,
    and all whitespace is removed so table-wrapped spans still match.
    """

    flattened = flatten_mineru_markup(unicodedata.normalize("NFKC", text))
    return "".join(flattened.split())


def validate_clinical_extraction(payload: Any, ocr_text: str) -> ClinicalExtraction:
    """Strict gates for provider output; rejects anything untrustworthy."""

    try:
        extraction = ClinicalExtraction.model_validate(payload)
    except ValidationError as exc:
        raise ClinicalExtractionRejected(
            f"clinical extraction payload rejected: {exc.error_count()} schema errors"
        ) from exc

    floor = get_settings().ai_extraction_confidence_floor
    weak_items = [
        item.raw_drug_name
        for item in extraction.prescription.items
        if item.confidence < floor
    ]
    if extraction.confidence < floor or weak_items:
        raise ClinicalExtractionRejected(
            "clinical extraction confidence is below the configured floor",
            issue_code=ISSUE_LOW_CONFIDENCE,
        )

    haystack = evidence_key(ocr_text or "")
    for item in extraction.prescription.items:
        if evidence_key(item.original_text) not in haystack:
            raise ClinicalExtractionRejected(
                f"prescription item evidence is not traceable to OCR text: "
                f"{item.raw_drug_name}",
                issue_code=ISSUE_EVIDENCE_MISSING,
            )
    return extraction


def extraction_to_parsed_document(
    extraction: ClinicalExtraction,
    *,
    template: str,
    parser_version: str,
) -> ParsedDocument:
    """Deterministic conversion into the shared candidate-fact shape.

    Decision-relevant warnings are recomputed from the structured fields
    here — never from the AI's own claims — so the validator downstream sees
    exactly the same fail-closed issue codes as the deterministic path.
    """

    encounter = extraction.encounter
    items = [
        ParsedPrescriptionItem(
            original_text=item.original_text,
            is_basic_drug=item.is_basic_drug,
            raw_drug_name=item.raw_drug_name,
            processing_method=item.processing_method,
            explicit_package_dose_g=item.explicit_package_dose_g,
            package_count=item.package_count,
            original_package_unit=item.package_unit,
            normalized_package_unit="package" if item.package_unit else None,
            suggested_canonical_name=item.suggested_canonical_name,
            # Legacy slips print plain per-herb grams (金银花 10g) with no
            # packaging; the printed gram value is itself the per-decoction
            # dose. Derived deterministically, not by the AI.
            is_direct_gram_dose=(
                item.package_count is None
                and item.package_unit is None
                and item.explicit_package_dose_g is not None
            ),
        )
        for item in extraction.prescription.items
    ]

    warnings: list[str] = []
    if extraction.patient.real_name is None:
        warnings.append("PATIENT_NAME_MISSING")
    if extraction.patient.medical_record_number is None:
        warnings.append("MEDICAL_RECORD_NUMBER_MISSING")
    if encounter.encounter_date is None:
        warnings.append("ENCOUNTER_DATE_MISSING")
    if not encounter.hospital:
        warnings.append("HOSPITAL_MISSING")
    if extraction.patient.sex == "unknown":
        warnings.append("SEX_UNKNOWN")
    if not items:
        warnings.append("PRESCRIPTION_NOT_FOUND")
    if any(
        item.package_count is not None and item.original_package_unit is None for item in items
    ):
        warnings.append("PACKAGE_UNIT_MISSING_IN_OCR")

    recognized: list[str] = []
    diagnostics: list[str] = []
    for warning in extraction.extraction_warnings:
        if warning in RECOGNIZED_WARNING_CODES:
            recognized.append(warning)
        else:
            diagnostics.append(warning)
    warnings.extend(recognized)

    return ParsedDocument(
        template=template,
        parser_version=parser_version,
        hospital=encounter.hospital or "",
        real_name=extraction.patient.real_name,
        sex=Sex(extraction.patient.sex),
        age=extraction.patient.age,
        encounter_date=encounter.encounter_date,
        medical_record_number=extraction.patient.medical_record_number,
        department=encounter.department,
        physician=encounter.physician,
        chief_complaint=encounter.chief_complaint,
        present_illness=encounter.present_illness,
        past_history=encounter.past_history,
        allergy=encounter.allergy,
        vital_signs=encounter.vital_signs,
        advice=encounter.advice,
        treatment_record=encounter.treatment_record,
        symptoms=[
            ParsedSymptom(
                raw=symptom.text,
                normalized=symptom.text,
                status=SymptomStatus(symptom.status),
                trend=(
                    SymptomTrend(symptom.trend)
                    if symptom.trend is not None and symptom.trend != "unknown"
                    else None
                ),
            )
            for symptom in extraction.symptoms
        ],
        diagnoses=[
            ParsedDiagnosis(
                raw=diagnosis.text,
                normalized=diagnosis.text,
                diagnosis_type=DiagnosisType(diagnosis.diagnosis_type),
                is_primary=diagnosis.is_primary,
            )
            for diagnosis in extraction.diagnoses
        ],
        prescription_items=items,
        total_decoctions=extraction.prescription.total_decoctions,
        raw_instruction_text=extraction.prescription.raw_text,
        warnings=warnings,
        extraction_warnings=diagnostics,
        structuring_source=STRUCTURING_SOURCE_AI,
    )


class ClinicalExtractionProvider(Protocol):
    """Provider-neutral extraction contract for the unified AI stage."""

    name: str

    def extract(self, ocr_text: str) -> dict[str, Any]:
        """Return a raw payload for ``validate_clinical_extraction``."""

    def verify(
        self,
        ocr_text: str,
        payload: dict[str, Any],
        blocking_issue_codes: list[str],
    ) -> dict[str, Any] | None:
        """Optional second pass against the original OCR (never new inference).

        Called only after the validator raised blocking issues and only when
        ``ai_verification_enabled`` is on. Returns a corrected payload or
        ``None`` when the second pass cannot safely confirm a correction.
        """


class MockClinicalExtractionProvider:
    """Deterministic fixture-backed test double; never installed for real data.

    Payloads live in a JSON registry keyed by ``evidence_key(ocr_text)``
    SHA-256 so a fixture can only ever answer for the exact OCR text it was
    annotated against. A missing entry fails closed instead of inventing an
    extraction.
    """

    name = "mock"

    def __init__(self, registry_path: str | Path | None = None) -> None:
        self._registry_path = Path(registry_path or get_settings().ai_extraction_fixture_path)
        self._entries: dict[str, dict[str, Any]] | None = None

    def _load_entries(self) -> dict[str, dict[str, Any]]:
        if self._entries is not None:
            return self._entries
        try:
            payload = json.loads(self._registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ClinicalExtractionRejected(
                f"clinical extraction fixture registry is unavailable: {exc}",
                issue_code=ISSUE_PROVIDER_FAILED,
            ) from exc
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            raise ClinicalExtractionRejected(
                "clinical extraction fixture registry must contain an entries object",
                issue_code=ISSUE_PROVIDER_FAILED,
            )
        self._entries = entries
        return entries

    def extract(self, ocr_text: str) -> dict[str, Any]:
        key = hashlib.sha256(evidence_key(ocr_text or "").encode("utf-8")).hexdigest()
        entry = self._load_entries().get(key)
        if entry is None:
            raise ClinicalExtractionRejected(
                "no mock extraction fixture matches this OCR text; configure a real "
                "provider or annotate a fixture",
                issue_code=ISSUE_PROVIDER_FAILED,
            )
        return entry

    def verify(
        self,
        ocr_text: str,
        payload: dict[str, Any],
        blocking_issue_codes: list[str],
    ) -> dict[str, Any] | None:
        # The mock never "corrects" anything: verification stays an explicit,
        # provider-specific capability.
        return None


def get_clinical_extraction_provider() -> ClinicalExtractionProvider:
    """Resolve the configured provider.

    Priority: environment-configured live provider, then the runtime
    ``provider_settings`` slot maintained by the settings UI (Phase 6.1),
    then the fail-closed mock fixture provider. A database lookup failure
    never breaks the pipeline — it degrades to the mock provider, which
    fails closed on unknown inputs.
    """

    settings = get_settings()
    if settings.ai_extraction_provider == "live_openai_compatible":
        from app.services.ai.live_provider import LiveOpenAICompatibleProvider

        return LiveOpenAICompatibleProvider()
    if settings.ai_extraction_provider == "mock":
        try:
            from app.db.session import SessionLocal
            from app.services.ai.live_provider import LiveOpenAICompatibleProvider
            from app.services.ai.provider_config import resolve_extraction_provider_params

            with SessionLocal() as session:
                params = resolve_extraction_provider_params(session)
            if params is not None:
                return LiveOpenAICompatibleProvider(
                    base_url=params["base_url"],
                    model=params["model"],
                    api_key=params["api_key"],
                )
        except Exception:  # noqa: BLE001 - config lookup must never break ingestion
            logger.warning(
                "Runtime provider settings lookup failed; falling back to mock provider",
                exc_info=True,
            )
        return MockClinicalExtractionProvider()
    raise ClinicalExtractionRejected(
        "no clinical extraction provider is configured for "
        f"'{settings.ai_extraction_provider}'",
        issue_code=ISSUE_PROVIDER_FAILED,
    )
