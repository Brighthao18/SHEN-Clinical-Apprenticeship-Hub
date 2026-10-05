"""Phase 6 ClinicalAIExtractor gates and the AI-primary ingestion chain.

Covers the non-negotiable rules: strict schema, evidence traceability,
missing-means-NULL, dictionary-gated drug identity, deterministic dose,
deterministic patient matching, IMPORTED/UNRESOLVED terminals, and the
preserved deterministic fallback path.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.domain import (
    Document,
    DrugDictionary,
    Encounter,
    Patient,
    PrescriptionItem,
    UnresolvedIngestion,
)
from app.services.ai.extractor import (
    ISSUE_EVIDENCE_MISSING,
    ISSUE_LOW_CONFIDENCE,
    ISSUE_PROVIDER_FAILED,
    ISSUE_SCHEMA_REJECTED,
    ClinicalExtractionRejected,
    evidence_key,
    extraction_to_parsed_document,
    validate_clinical_extraction,
)
from app.services.drugs import resolve_drug_with_suggestion
from app.services.parsers.detector import CURRENT_TEMPLATE

FIXTURE_OCR = (Path(__file__).parent / "fixtures" / "jnu_outpatient_v1.txt").read_text(
    encoding="utf-8"
)
DEFAULT_REGISTRY = (
    Path(__file__).parent / "fixtures" / "ai_extraction_fixtures.json"
)


def _base_payload() -> dict:
    registry = json.loads(DEFAULT_REGISTRY.read_text(encoding="utf-8"))
    key = hashlib.sha256(evidence_key(FIXTURE_OCR).encode("utf-8")).hexdigest()
    return deepcopy(registry["entries"][key])


def _write_registry(tmp_path: Path, ocr_text: str, payload: dict) -> Path:
    key = hashlib.sha256(evidence_key(ocr_text).encode("utf-8")).hexdigest()
    path = tmp_path / "registry.json"
    path.write_text(
        json.dumps({"version": 1, "entries": {key: payload}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _use_registry(monkeypatch, tmp_path: Path, ocr_text: str, payload: dict) -> None:
    path = _write_registry(tmp_path, ocr_text, payload)
    monkeypatch.setattr(get_settings(), "ai_extraction_fixture_path", path, raising=False)


# ---------------------------------------------------------------------------
# Extractor gates
# ---------------------------------------------------------------------------


def test_strict_schema_rejects_extra_fields() -> None:
    payload = _base_payload()
    payload["free_form_markdown_summary"] = "The AI felt good about this case."
    with pytest.raises(ClinicalExtractionRejected) as excinfo:
        validate_clinical_extraction(payload, FIXTURE_OCR)
    assert excinfo.value.issue_code == ISSUE_SCHEMA_REJECTED


def test_masked_medical_record_number_is_rejected() -> None:
    payload = _base_payload()
    payload["patient"]["medical_record_number"] = "*1234*"
    with pytest.raises(ClinicalExtractionRejected) as excinfo:
        validate_clinical_extraction(payload, FIXTURE_OCR)
    assert excinfo.value.issue_code == ISSUE_SCHEMA_REJECTED


def test_non_cjk_drug_name_is_rejected() -> None:
    payload = _base_payload()
    payload["prescription"]["items"][0]["raw_drug_name"] = "Bie Jia"
    with pytest.raises(ClinicalExtractionRejected):
        validate_clinical_extraction(payload, FIXTURE_OCR)


def test_evidence_must_be_traceable_to_ocr_text() -> None:
    payload = _base_payload()
    payload["prescription"]["items"][0]["original_text"] = "（基）醋鳖甲（10g）3包"
    with pytest.raises(ClinicalExtractionRejected) as excinfo:
        validate_clinical_extraction(payload, FIXTURE_OCR)
    assert excinfo.value.issue_code == ISSUE_EVIDENCE_MISSING


def test_evidence_survives_table_markup_and_width_differences() -> None:
    payload = _base_payload()
    payload["prescription"]["items"] = [payload["prescription"]["items"][0]]
    ocr_with_table = "<table><tr><td>（基）醋鳖甲（10g）2包</td></tr></table>"
    validate_clinical_extraction(payload, ocr_with_table)


def test_low_confidence_fails_closed() -> None:
    payload = _base_payload()
    payload["confidence"] = 0.5
    with pytest.raises(ClinicalExtractionRejected) as excinfo:
        validate_clinical_extraction(payload, FIXTURE_OCR)
    assert excinfo.value.issue_code == ISSUE_LOW_CONFIDENCE


def test_conversion_derives_flags_deterministically_and_keeps_missing_as_null() -> None:
    payload = _base_payload()
    payload["prescription"]["items"].append(
        {
            "original_text": "金银花 10g",
            "raw_drug_name": "金银花",
            "processing_method": None,
            "suggested_canonical_name": None,
            "is_basic_drug": False,
            "explicit_package_dose_g": 10,
            "package_count": None,
            "package_unit": None,
            "confidence": 0.95,
        }
    )
    extraction = validate_clinical_extraction(payload, FIXTURE_OCR + "\n金银花 10g")
    parsed = extraction_to_parsed_document(
        extraction,
        template=CURRENT_TEMPLATE,
        parser_version="ai-clinical/test",
    )
    direct = parsed.prescription_items[-1]
    assert direct.is_direct_gram_dose is True
    assert direct.package_count is None
    assert direct.calculated_explicit_dose_g == 10
    # Missing source content stays NULL; nothing was inferred.
    assert parsed.present_illness is None
    assert parsed.vital_signs is None
    assert parsed.structuring_source == "ai_extracted"


def test_unrecognized_ai_warnings_never_become_decisions() -> None:
    payload = _base_payload()
    payload["extraction_warnings"] = [
        "MULTIPLE_PATIENT_NAMES_SUSPECTED",
        " handwriting looks sloppy ",
    ]
    extraction = validate_clinical_extraction(payload, FIXTURE_OCR)
    parsed = extraction_to_parsed_document(
        extraction, template=CURRENT_TEMPLATE, parser_version="ai-clinical/test"
    )
    # Recognized vocabulary only ever adds blocking signals downstream.
    assert "MULTIPLE_PATIENT_NAMES_SUSPECTED" in parsed.warnings
    # Unrecognized diagnostics are stored, never mapped to a decision.
    assert parsed.extraction_warnings == [" handwriting looks sloppy "]
    assert " handwriting looks sloppy " not in parsed.warnings


def test_mock_provider_fails_closed_without_fixture(monkeypatch, tmp_path) -> None:
    _use_registry(monkeypatch, tmp_path, "完全不相关的文本", _base_payload())
    from app.services.ai.extractor import MockClinicalExtractionProvider

    with pytest.raises(ClinicalExtractionRejected) as excinfo:
        MockClinicalExtractionProvider().extract(FIXTURE_OCR)
    assert excinfo.value.issue_code == ISSUE_PROVIDER_FAILED


# ---------------------------------------------------------------------------
# Dictionary gate (AI suggestion never creates canonical drugs)
# ---------------------------------------------------------------------------


def _seed_canonical_drug(db_session, canonical: str, processing: str | None) -> None:
    from datetime import date

    db_session.add(
        DrugDictionary(
            canonical_name=canonical,
            processing_method=processing,
            aliases=[],
            hospital_display_names=[canonical],
            common_ocr_errors=[],
            dictionary_version=get_settings().drug_dictionary_version,
            effective_from=date(2026, 1, 1),
            source="test",
        )
    )
    db_session.flush()


def test_suggestion_resolves_only_through_existing_dictionary(db_session) -> None:
    # No 银花 entry: the suggestion must NOT create it.
    result = resolve_drug_with_suggestion(db_session, "银花", None, "金银花")
    assert result.dictionary is None
    assert result.canonical_name is None

    _seed_canonical_drug(db_session, "金银花", None)
    result = resolve_drug_with_suggestion(db_session, "银花", None, "金银花")
    assert result.dictionary is not None
    assert result.canonical_name == "金银花"
    assert db_session.scalar(select(func.count(DrugDictionary.id))) == 1


def test_direct_dictionary_hit_does_not_require_suggestion(db_session) -> None:
    result = resolve_drug_with_suggestion(db_session, "醋鳖甲", None, None)
    assert result.canonical_name == "鳖甲"
    assert result.processing_method == "醋制"


# ---------------------------------------------------------------------------
# AI-primary pipeline
# ---------------------------------------------------------------------------


def _upload(client, auth_headers, ocr_text: str, label: str):
    return client.post(
        "/api/v1/imports",
        headers=auth_headers,
        files={"files": ("visit.jpg", b"ai-pipeline-fixture", "image/jpeg")},
        data={"mock_ocr_text": ocr_text, "label": label},
    )


def test_dictionary_coverage_gap_imports_with_pending_normalization(
    client, auth_headers, monkeypatch, tmp_path
) -> None:
    """Phase 6.1: a traceable raw name without a canonical mapping no longer
    blocks the encounter; the item is stored marked as pending."""

    ocr_text = FIXTURE_OCR + "\n威灵仙（10g）1包"
    payload = _base_payload()
    payload["prescription"]["raw_text"] = "共7剂\n每日1剂"
    payload["prescription"]["items"].append(
        {
            "original_text": "威灵仙（10g）1包",
            "raw_drug_name": "威灵仙",
            "processing_method": None,
            "suggested_canonical_name": "威灵仙",
            "is_basic_drug": False,
            "explicit_package_dose_g": 10,
            "package_count": 1,
            "package_unit": "包",
            "confidence": 0.97,
        }
    )
    _use_registry(monkeypatch, tmp_path, ocr_text, payload)

    response = _upload(client, auth_headers, ocr_text, "词典覆盖缺口")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "ACCEPTED"

    with SessionLocal() as session:
        item = session.scalar(
            select(PrescriptionItem).where(PrescriptionItem.raw_drug_name == "威灵仙")
        )
        assert item is not None
        assert item.normalization_status == "pending"
        assert item.canonical_drug_name is None
        assert item.ai_suggested_canonical_name == "威灵仙"
        # Dose arithmetic stays deterministic even without canonical mapping.
        assert item.calculated_dose_g == 10
        assert session.scalar(select(func.count(Encounter.id))) == 1


def test_unverifiable_raw_drug_identity_blocks_to_unresolved(
    client, auth_headers, monkeypatch, tmp_path
) -> None:
    """Phase 6.1: raw identity itself unproven (raw name absent from its own
    cited OCR evidence) still blocks the whole document."""

    ocr_text = FIXTURE_OCR + "\n威灵仙（10g）1包"
    payload = _base_payload()
    payload["prescription"]["raw_text"] = "共7剂\n每日1剂"
    payload["prescription"]["items"].append(
        {
            # raw name invents a different herb than the cited OCR span.
            "original_text": "威灵仙（10g）1包",
            "raw_drug_name": "五灵脂",
            "processing_method": None,
            "suggested_canonical_name": None,
            "is_basic_drug": False,
            "explicit_package_dose_g": 10,
            "package_count": 1,
            "package_unit": "包",
            "confidence": 0.97,
        }
    )
    _use_registry(monkeypatch, tmp_path, ocr_text, payload)

    response = _upload(client, auth_headers, ocr_text, "身份不可证")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "UNRESOLVED"

    with SessionLocal() as session:
        assert session.scalar(select(func.count(Patient.id))) == 0
        assert session.scalar(select(func.count(Encounter.id))) == 0
        assert session.scalar(select(func.count(PrescriptionItem.id))) == 0
        ledger = session.scalar(select(UnresolvedIngestion))
        assert ledger is not None
        assert ledger.failure_reason_code == "DRUG_IDENTITY_UNVERIFIABLE"


def test_ai_suggestion_imports_when_dictionary_already_has_canonical(
    client, auth_headers, monkeypatch, tmp_path, db_session
) -> None:
    from datetime import date

    from app.models.domain import DrugDictionary

    db_session.add(
        DrugDictionary(
            canonical_name="延胡索",
            processing_method="醋制",
            aliases=[],
            hospital_display_names=["醋延胡索"],
            common_ocr_errors=[],
            dictionary_version=get_settings().drug_dictionary_version,
            effective_from=date(2026, 1, 1),
            source="test",
        )
    )
    db_session.commit()

    ocr_text = FIXTURE_OCR + "\n醋延胡索（10g）1包"
    payload = _base_payload()
    payload["prescription"]["raw_text"] = "共7剂\n每日1剂"
    payload["prescription"]["items"].append(
        {
            "original_text": "醋延胡索（10g）1包",
            "raw_drug_name": "醋延胡索",
            "processing_method": "醋制",
            "suggested_canonical_name": "延胡索",
            "is_basic_drug": False,
            "explicit_package_dose_g": 10,
            "package_count": 1,
            "package_unit": "包",
            "confidence": 0.97,
        }
    )
    _use_registry(monkeypatch, tmp_path, ocr_text, payload)

    response = _upload(client, auth_headers, ocr_text, "词典内建议")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "ACCEPTED"

    with SessionLocal() as session:
        item = session.scalar(
            select(PrescriptionItem).where(PrescriptionItem.raw_drug_name == "醋延胡索")
        )
        assert item is not None
        assert item.canonical_drug_name == "延胡索"
        assert item.processing_method == "醋制"
        assert item.calculated_dose_g == 10


def test_missing_encounter_date_blocks_to_unresolved(
    client, auth_headers, monkeypatch, tmp_path
) -> None:
    ocr_text = FIXTURE_OCR.replace("日期：2026-08-14", "日期：")
    payload = _base_payload()
    payload["encounter"]["encounter_date"] = None
    _use_registry(monkeypatch, tmp_path, ocr_text, payload)

    response = _upload(client, auth_headers, ocr_text, "缺少日期")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "UNRESOLVED"

    with SessionLocal() as session:
        assert session.scalar(select(func.count(Encounter.id))) == 0
        ledger = session.scalar(select(UnresolvedIngestion))
        assert ledger is not None
        assert ledger.failure_reason_code == "ENCOUNTER_DATE_MISSING"


def test_duplicated_prescription_item_blocks_to_unresolved(
    client, auth_headers, monkeypatch, tmp_path
) -> None:
    ocr_text = FIXTURE_OCR + "\n茯神（5g）3包"
    payload = _base_payload()
    payload["prescription"]["items"].append(deepcopy(payload["prescription"]["items"][3]))
    _use_registry(monkeypatch, tmp_path, ocr_text, payload)

    response = _upload(client, auth_headers, ocr_text, "重复项")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "UNRESOLVED"

    with SessionLocal() as session:
        ledger = session.scalar(select(UnresolvedIngestion))
        assert ledger is not None
        assert ledger.failure_reason_code == "DUPLICATED_PRESCRIPTION_ITEM"


def test_ai_provenance_is_never_physician_record(
    client, auth_headers, monkeypatch, tmp_path
) -> None:
    _use_registry(monkeypatch, tmp_path, FIXTURE_OCR, _base_payload())
    response = _upload(client, auth_headers, FIXTURE_OCR, "来源检查")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "ACCEPTED"

    with SessionLocal() as session:
        document = session.scalar(select(Document))
        assert document is not None
        snapshot = document.structured_snapshot
        assert snapshot["structuring_source"] == "ai_extracted"
        assert document.parser_version.startswith("ai-clinical/")
        from app.models.domain import Diagnosis, Prescription

        diagnosis = session.scalar(select(Diagnosis))
        assert diagnosis is not None
        assert diagnosis.source_type.value == "ai_normalized"
        prescription = session.scalar(select(Prescription))
        assert prescription is not None
        assert prescription.source_type.value == "ai_normalized"


def test_deterministic_fallback_path_still_works(client, auth_headers, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_extraction_enabled", False, raising=False)

    response = _upload(client, auth_headers, FIXTURE_OCR, "回退路径")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "ACCEPTED"
    with SessionLocal() as session:
        document = session.scalar(select(Document))
        assert document is not None
        assert document.parser_template == CURRENT_TEMPLATE
        assert document.parser_version == get_settings().jnu_parser_version
        snapshot = document.structured_snapshot
        assert snapshot["structuring_source"] == "deterministic_parser"
        from app.models.domain import Diagnosis

        diagnosis = session.scalar(select(Diagnosis))
        assert diagnosis is not None
        assert diagnosis.source_type.value == "physician_record"


def test_fallback_unknown_template_fails_closed_as_before(
    client, auth_headers, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_extraction_enabled", False, raising=False)
    response = _upload(client, auth_headers, "普通购物小票\n合计 42 元", "回退未知模板")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "UNRESOLVED"
    with SessionLocal() as session:
        ledger = session.scalar(select(UnresolvedIngestion))
        assert ledger is not None
        assert ledger.failure_reason_code == "UNSUPPORTED_TEMPLATE"


def test_enabled_second_pass_keeps_fail_closed_without_correction(
    client, auth_headers, monkeypatch, tmp_path
) -> None:
    ocr_text = FIXTURE_OCR.replace("日期：2026-08-14", "日期：")
    payload = _base_payload()
    payload["encounter"]["encounter_date"] = None
    _use_registry(monkeypatch, tmp_path, ocr_text, payload)
    monkeypatch.setattr(get_settings(), "ai_verification_enabled", True, raising=False)

    response = _upload(client, auth_headers, ocr_text, "二次核对")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "UNRESOLVED"
    with SessionLocal() as session:
        ledger = session.scalar(select(UnresolvedIngestion))
        assert ledger is not None
        # Mock verification never corrects anything: the first-pass blocking
        # decision stands.
        assert ledger.failure_reason_code == "ENCOUNTER_DATE_MISSING"
