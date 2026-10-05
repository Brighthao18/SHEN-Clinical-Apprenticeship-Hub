"""Pipeline failure/assist paths beyond the happy chain.

Covers: the fail-closed mock OCR terminal, retryable OCR provider failures
with retry scheduling, the optional second-pass verification re-check, and
LLM-assisted legacy structuring (disabled / rejected / accepted). All
provider doubles are deterministic fixtures; none of these tests make
AI-accuracy claims.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models.domain import (
    Document,
    DrugDictionary,
    Encounter,
    OCRProviderSubmission,
    Patient,
    Prescription,
    PrescriptionItem,
    ProcessingJob,
    UnresolvedIngestion,
)
from app.models.enums import DocumentType, JobStatus, ProcessingStatus, SourceType
from app.services.legacy_structuring import (
    STRUCTURING_SOURCE_LLM,
    MockLegacyStructuringProvider,
)
from app.services.ocr.base import OCRProviderError
from app.services.pipeline import PipelineRetryableError, PipelineService

FIXTURE_OCR = (Path(__file__).parent / "fixtures" / "jnu_outpatient_v1.txt").read_text(
    encoding="utf-8"
)
DEFAULT_REGISTRY = Path(__file__).parent / "fixtures" / "ai_extraction_fixtures.json"
LEGACY_OCR_UNPARSEABLE = "中药处方笺\nRp\n没有任何字段"


def _base_payload() -> dict:
    registry = json.loads(DEFAULT_REGISTRY.read_text(encoding="utf-8"))
    from app.services.ai.extractor import evidence_key

    key = hashlib.sha256(evidence_key(FIXTURE_OCR).encode("utf-8")).hexdigest()
    return deepcopy(registry["entries"][key])


STORAGE_ROOT = Path(__file__).parents[3] / "work" / "test-storage"


def _document(label: str, ocr_text: str | None = None) -> Document:
    digest = hashlib.sha256(label.encode()).hexdigest()
    return Document(
        id=uuid.uuid4(),
        original_filename=f"{label}.jpg",
        original_file_path=f"synthetic/{digest}.jpg",
        sha256=digest,
        byte_size=10,
        mime_type="image/jpeg",
        document_type=DocumentType.OUTPATIENT_RECORD,
        processing_status=ProcessingStatus.UPLOADED,
        ocr_text=ocr_text,
    )


def _materialize_source_file(document: Document) -> None:
    path = STORAGE_ROOT / document.original_file_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic-image-bytes")


def _seed_canonical_drug(db_session, canonical: str) -> None:
    from datetime import date

    db_session.add(
        DrugDictionary(
            canonical_name=canonical,
            processing_method=None,
            candidate_raw_name=canonical,
            aliases=[],
            hospital_display_names=[canonical],
            common_ocr_errors=[],
            dictionary_version=get_settings().drug_dictionary_version,
            effective_from=date(2026, 1, 1),
            source="test",
            status="VERIFIED",
        )
    )
    db_session.flush()


# ---------------------------------------------------------------------------
# OCR provider failure paths
# ---------------------------------------------------------------------------


def test_mock_ocr_without_explicit_text_fails_closed(db_session) -> None:
    """The mock provider never fabricates content: no fixture text means a
    terminal FAILED with a recorded provider-submission failure."""

    document = _document("no-ocr-text")
    _materialize_source_file(document)
    db_session.add(document)
    db_session.commit()

    result = PipelineService(db_session).process(document.id)

    assert result.status == ProcessingStatus.FAILED
    db_session.expire_all()
    document = db_session.get(Document, document.id)
    assert document is not None
    assert document.processing_status == ProcessingStatus.FAILED
    assert document.failure_code == "MOCK_OCR_TEXT_REQUIRED"
    submission = db_session.scalar(select(OCRProviderSubmission))
    assert submission is not None
    assert submission.status == "failed"
    assert submission.failure_code == "MOCK_OCR_TEXT_REQUIRED"
    job = db_session.scalar(select(ProcessingJob))
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert db_session.scalar(select(func.count(Encounter.id))) == 0


def test_retryable_ocr_failure_schedules_retry_and_keeps_document(
    db_session, monkeypatch
) -> None:
    class _FlakyOCRProvider:
        name = "flaky"

        def extract(self, path, *, explicit_text=None, on_submitted=None):
            raise OCRProviderError("OCR_PROVIDER_TIMEOUT", "simulated timeout", retryable=True)

    monkeypatch.setattr(
        "app.services.pipeline.get_ocr_provider", lambda: _FlakyOCRProvider()
    )
    document = _document("retryable-ocr")
    _materialize_source_file(document)
    db_session.add(document)
    db_session.commit()

    with pytest.raises(PipelineRetryableError):
        PipelineService(db_session).process(document.id)

    db_session.expire_all()
    document = db_session.get(Document, document.id)
    assert document is not None
    assert document.processing_status == ProcessingStatus.QUEUED
    assert document.failure_code == "OCR_PROVIDER_TIMEOUT"
    job = db_session.scalar(select(ProcessingJob))
    assert job is not None
    assert job.status == JobStatus.RETRY_SCHEDULED
    assert job.attempt == 1
    submission = db_session.scalar(select(OCRProviderSubmission))
    assert submission is not None
    assert submission.status == "failed"


# ---------------------------------------------------------------------------
# Optional second-pass verification (one re-check against the original OCR)
# ---------------------------------------------------------------------------


class _ScriptedExtractionProvider:
    name = "fixture"

    def __init__(self, extract_payload: dict, verified_payload: dict | None) -> None:
        self._extract_payload = extract_payload
        self._verified_payload = verified_payload

    def extract(self, ocr_text: str) -> dict:
        return deepcopy(self._extract_payload)

    def verify(self, ocr_text: str, payload: dict, codes: list[str]) -> dict | None:
        return deepcopy(self._verified_payload)


def test_second_pass_verification_corrects_and_imports(
    db_session, monkeypatch
) -> None:
    """A blocking gap may be re-checked once against the original OCR; the
    corrected payload re-enters the SAME validator and normal chain."""

    monkeypatch.setattr(get_settings(), "ai_verification_enabled", True)
    payload = _base_payload()
    corrected = deepcopy(payload)
    payload["encounter"]["encounter_date"] = None
    monkeypatch.setattr(
        "app.services.pipeline.get_clinical_extraction_provider",
        lambda: _ScriptedExtractionProvider(payload, corrected),
    )
    document = _document("second-pass", FIXTURE_OCR)
    db_session.add(document)
    db_session.commit()

    result = PipelineService(db_session).process(document.id)

    assert result.status == ProcessingStatus.ACCEPTED
    assert result.encounter_id is not None
    db_session.expire_all()
    snapshot = db_session.get(Document, document.id).structured_snapshot
    assert snapshot["encounter_date"] == "2026-08-14"
    assert db_session.scalar(select(func.count(Encounter.id))) == 1


def test_second_pass_verification_decline_keeps_unresolved(
    db_session, monkeypatch
) -> None:
    """An unclear re-check corrects nothing: the original blocking issue
    still routes the document to UNRESOLVED."""

    monkeypatch.setattr(get_settings(), "ai_verification_enabled", True)
    payload = _base_payload()
    payload["encounter"]["encounter_date"] = None
    monkeypatch.setattr(
        "app.services.pipeline.get_clinical_extraction_provider",
        lambda: _ScriptedExtractionProvider(payload, None),
    )
    document = _document("second-pass-decline", FIXTURE_OCR)
    db_session.add(document)
    db_session.commit()

    result = PipelineService(db_session).process(document.id)

    assert result.status == ProcessingStatus.UNRESOLVED
    db_session.expire_all()
    assert db_session.scalar(select(func.count(Encounter.id))) == 0
    ledger = db_session.scalar(select(UnresolvedIngestion))
    assert ledger is not None
    assert ledger.failure_reason_code == "ENCOUNTER_DATE_MISSING"


# ---------------------------------------------------------------------------
# LLM-assisted legacy structuring (disabled by default; OD-010)
# ---------------------------------------------------------------------------


def _legacy_structuring_payload() -> dict:
    return {
        "real_name": "测试患者乙",
        "sex": "female",
        "age": 55,
        "encounter_date": "2026-07-10",
        "medical_record_number": "LEG-0001",
        "department": "中医科门诊",
        "physician": "沈老师",
        "diagnoses": ["胃痞"],
        "items": [
            {
                "original_text": "金银花 10g",
                "raw_drug_name": "金银花",
                "processing_method": None,
                "explicit_dose_g": 10,
                "package_count": None,
                "package_unit": None,
                "confidence": 0.95,
            }
        ],
        "total_decoctions": 7,
        "confidence": 0.92,
    }


def _disable_ai_extraction(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "ai_extraction_enabled", False)


def test_legacy_assist_disabled_stays_incomplete(db_session, monkeypatch) -> None:
    _disable_ai_extraction(monkeypatch)
    document = _document("legacy-no-assist", LEGACY_OCR_UNPARSEABLE)
    db_session.add(document)
    db_session.commit()

    result = PipelineService(db_session).process(document.id)

    assert result.status == ProcessingStatus.UNRESOLVED
    db_session.expire_all()
    ledger = db_session.scalar(select(UnresolvedIngestion))
    assert ledger is not None
    assert ledger.failure_reason_code == "LEGACY_PARSER_INCOMPLETE"
    assert db_session.scalar(select(func.count(Patient.id))) == 0


def test_legacy_llm_structuring_rejected_blocks_unresolved(
    db_session, monkeypatch
) -> None:
    _disable_ai_extraction(monkeypatch)
    monkeypatch.setattr(get_settings(), "legacy_llm_structuring_enabled", True)
    monkeypatch.setattr(
        "app.services.pipeline.get_legacy_structuring_provider",
        lambda: MockLegacyStructuringProvider(None),
    )
    document = _document("legacy-rejected", LEGACY_OCR_UNPARSEABLE)
    db_session.add(document)
    db_session.commit()

    result = PipelineService(db_session).process(document.id)

    assert result.status == ProcessingStatus.UNRESOLVED
    db_session.expire_all()
    ledger = db_session.scalar(select(UnresolvedIngestion))
    assert ledger is not None
    assert ledger.failure_reason_code == "LLM_STRUCTURING_SCHEMA_REJECTED"
    assert db_session.scalar(select(func.count(Patient.id))) == 0
    assert db_session.scalar(select(func.count(Encounter.id))) == 0


def test_legacy_llm_structuring_success_reenters_normal_gates(
    db_session, monkeypatch
) -> None:
    """A valid legacy payload flows through drug resolution, dose
    arithmetic, matching, and the validator like any other extraction; its
    provenance is ai_normalized, never a physician record."""

    _disable_ai_extraction(monkeypatch)
    monkeypatch.setattr(get_settings(), "legacy_llm_structuring_enabled", True)
    monkeypatch.setattr(
        "app.services.pipeline.get_legacy_structuring_provider",
        lambda: MockLegacyStructuringProvider(_legacy_structuring_payload()),
    )
    _seed_canonical_drug(db_session, "金银花")
    document = _document("legacy-llm", LEGACY_OCR_UNPARSEABLE)
    db_session.add(document)
    db_session.commit()

    result = PipelineService(db_session).process(document.id)

    assert result.status == ProcessingStatus.ACCEPTED, (
        db_session.scalar(select(UnresolvedIngestion)).failure_reason_code
        if result.status == ProcessingStatus.UNRESOLVED
        else ""
    )
    db_session.expire_all()
    item = db_session.scalar(select(PrescriptionItem))
    assert item is not None
    assert item.raw_drug_name == "金银花"
    assert item.canonical_drug_name == "金银花"
    assert item.calculated_dose_g == 10
    assert item.dose_scope.value == "per_decoction"
    assert item.normalization_status == "normalized"
    item_row = db_session.scalar(select(PrescriptionItem))
    prescription = db_session.get(Prescription, item_row.prescription_id)
    assert prescription is not None
    assert prescription.source_type == SourceType.AI_NORMALIZED
    document = db_session.get(Document, document.id)
    assert document is not None
    assert document.structured_snapshot["structuring_source"] == STRUCTURING_SOURCE_LLM
    assert db_session.scalar(select(func.count(Encounter.id))) == 1
    patient = db_session.scalar(select(Patient))
    assert patient is not None
    identity = patient.identities[0]
    assert identity.hospital_medical_record_number == "LEG-0001"
