from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from app.models.enums import DiagnosisType, Sex, SymptomStatus, SymptomTrend


@dataclass(frozen=True)
class ParsedSymptom:
    raw: str
    normalized: str | None
    status: SymptomStatus
    trend: SymptomTrend | None = None


@dataclass(frozen=True)
class ParsedDiagnosis:
    raw: str
    normalized: str | None
    diagnosis_type: DiagnosisType
    is_primary: bool = False


@dataclass(frozen=True)
class ParsedPrescriptionItem:
    original_text: str
    is_basic_drug: bool
    raw_drug_name: str
    processing_method: str | None
    explicit_package_dose_g: Decimal | None
    package_count: Decimal | None
    original_package_unit: str | None
    normalized_package_unit: str | None
    # Legacy herbal slips print plain per-decoction grams (金银花 10g) with no
    # 包/袋 packaging; the printed gram value is itself the per-decoction dose.
    is_direct_gram_dose: bool = False
    # AI proposal only (Phase 6 extractor). Identity is still resolved
    # exclusively through the DrugDictionary; it never bypasses or extends it.
    suggested_canonical_name: str | None = None

    @property
    def calculated_explicit_dose_g(self) -> Decimal | None:
        if self.explicit_package_dose_g is None:
            return None
        if self.package_count is None:
            return self.explicit_package_dose_g if self.is_direct_gram_dose else None
        return self.explicit_package_dose_g * self.package_count


@dataclass(frozen=True)
class ParsedDocument:
    template: str
    parser_version: str
    hospital: str
    real_name: str | None
    sex: Sex
    age: int | None
    # Optional since the AI extractor may fail to find a printed date; the
    # validator blocks ingestion on ENCOUNTER_DATE_MISSING before commit, so
    # domain creation only ever runs with a concrete date.
    encounter_date: date | None
    medical_record_number: str | None
    department: str | None
    physician: str | None
    chief_complaint: str | None
    present_illness: str | None
    past_history: str | None
    allergy: str | None
    vital_signs: str | None
    advice: str | None
    treatment_record: str | None
    symptoms: list[ParsedSymptom] = field(default_factory=list)
    diagnoses: list[ParsedDiagnosis] = field(default_factory=list)
    prescription_items: list[ParsedPrescriptionItem] = field(default_factory=list)
    total_decoctions: int | None = None
    raw_instruction_text: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Advisory AI diagnostics outside the recognized warning vocabulary;
    # stored in the structured snapshot only, never a validation decision.
    extraction_warnings: list[str] = field(default_factory=list)
    # "deterministic_parser", "llm_assisted", or "ai_extracted"; drives
    # provenance so AI-structured records are never stored as physician
    # records.
    structuring_source: str = "deterministic_parser"
    # Phase 6.2 Vision Verification Fallback provenance: per-field records of
    # the constrained image re-read (source=ai_visual_verification). Stored
    # in the structured snapshot only; the patched values still pass through
    # the same validator, dictionary, dose calculator, and patient matcher.
    vision_verifications: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["sex"] = self.sex.value
        payload["encounter_date"] = (
            self.encounter_date.isoformat() if self.encounter_date else None
        )
        for symptom, symptom_source in zip(payload["symptoms"], self.symptoms, strict=True):
            symptom["status"] = symptom_source.status.value
            symptom["trend"] = (
                symptom_source.trend.value if symptom_source.trend else None
            )
        for diagnosis, diagnosis_source in zip(payload["diagnoses"], self.diagnoses, strict=True):
            diagnosis["diagnosis_type"] = diagnosis_source.diagnosis_type.value
        for item in payload["prescription_items"]:
            for key in ("explicit_package_dose_g", "package_count"):
                if item[key] is not None:
                    item[key] = str(item[key])
        return payload
