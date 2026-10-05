"""Phase 6.2 Vision Verification Fallback gates.

Covers the non-negotiable properties: off by default, whitelisted trigger
codes only, constrained per-field answers (unexpected fields reject the
whole verification), missing-means-NULL, one call per ingestion attempt,
provenance with source=ai_visual_verification, and re-entry through the
same validator with untouched dictionary/dose/matching.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.domain import (
    Document,
    Encounter,
    Patient,
    PrescriptionItem,
    UnresolvedIngestion,
    VisionVerification,
)
from app.services.ai.extractor import evidence_key
from app.services.ai.vision_verifier import (
    SOURCE_AI_VISUAL_VERIFICATION,
    VisionVerificationRejected,
    apply_vision_verifications,
    build_vision_field_requests,
    parse_vision_verification_response,
)
from app.services.parsers.types import ParsedDocument

IMAGE_BYTES = b"vision-fallback-fixture-image"


def _vision_ocr(with_mrn: bool = True) -> str:
    header = "姓名：测试甲  性别：男  年龄：58\n"
    if with_mrn:
        header += "病历号：0001112223\n"
    return (
        "暨南大学附属第一医院 门诊病历\n"
        + header
        + "科别：中医科  日期：2026-08-20\n"
        + "中医诊断：脾虚湿困\n"
        "处方：\n"
        "共7剂\n"
        "（基）茯神（5g）3\n"
        "（基）白术（10g）2包\n"
        "用法: 口服 每日1剂 每日2次 一次用量:150ml\n"
    )


def _payload(ocr_text: str, *, with_mrn: bool = True, with_date: bool = True) -> dict:
    return {
        "patient": {
            "real_name": "测试甲",
            "sex": "male",
            "age": 58,
            "medical_record_number": "0001112223" if with_mrn else None,
        },
        "encounter": {
            "hospital": "暨南大学附属第一医院",
            "department": "中医科",
            "physician": None,
            "encounter_date": "2026-08-20" if with_date else None,
            "chief_complaint": None,
            "present_illness": None,
            "past_history": None,
            "allergy": None,
            "vital_signs": None,
            "advice": None,
            "treatment_record": None,
        },
        "symptoms": [],
        "diagnoses": [
            {"text": "脾虚湿困", "diagnosis_type": "tcm_pattern", "is_primary": True}
        ],
        "prescription": {
            "total_decoctions": 7,
            "raw_text": "共7剂\n用法: 口服 每日1剂 每日2次 一次用量:150ml",
            "items": [
                {
                    # Count printed without a unit: the OCR-source gap.
                    "original_text": "（基）茯神（5g）3",
                    "raw_drug_name": "茯神",
                    "processing_method": None,
                    "suggested_canonical_name": "茯神",
                    "is_basic_drug": False,
                    "explicit_package_dose_g": 5,
                    "package_count": 3,
                    "package_unit": None,
                    "confidence": 0.97,
                },
                {
                    "original_text": "（基）白术（10g）2包",
                    "raw_drug_name": "白术",
                    "processing_method": None,
                    "suggested_canonical_name": "白术",
                    "is_basic_drug": False,
                    "explicit_package_dose_g": 10,
                    "package_count": 2,
                    "package_unit": "包",
                    "confidence": 0.97,
                },
            ],
        },
        "extraction_warnings": [],
        "confidence": 0.96,
    }


def _write_registry(tmp_path: Path, ocr_text: str, payload: dict) -> Path:
    key = hashlib.sha256(evidence_key(ocr_text).encode("utf-8")).hexdigest()
    path = tmp_path / "vision-registry.json"
    path.write_text(
        json.dumps({"version": 1, "entries": {key: payload}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _use_registry(monkeypatch, tmp_path: Path, ocr_text: str, payload: dict) -> None:
    path = _write_registry(tmp_path, ocr_text, payload)
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_extraction_fixture_path", path, raising=False)


def _enable_vision(monkeypatch, provider) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_vision_verification_enabled", True, raising=False)
    monkeypatch.setattr(
        settings, "allow_identifiable_ai_vision_upload", True, raising=False
    )
    if provider is not None:
        monkeypatch.setattr(
            "app.services.pipeline.get_vision_verification_provider", lambda: provider
        )


def _units_complete(payload: dict) -> dict:
    payload["prescription"]["items"][0]["package_unit"] = "包"
    return payload


class StubVisionProvider:
    """Deterministic plumbing stub; answers only the requested fields."""

    name = "stub_vision"

    def __init__(self, answers: dict[str, str | None], confidence: float = 0.99) -> None:
        self.answers = answers
        self.confidence = confidence
        self.calls: list[dict] = []

    def verify_fields(self, image_bytes: bytes, media_type: str, requests):
        self.calls.append(
            {
                "image_size": len(image_bytes),
                "media_type": media_type,
                "requested": [request.field_path for request in requests],
            }
        )
        verifications = []
        for request in requests:
            verifications.append(
                {
                    "field_path": request.field_path,
                    "read_value": self.answers.get(request.field_path),
                    "confidence": self.confidence,
                    "evidence_quote": None
                    if self.answers.get(request.field_path) is None
                    else str(self.answers.get(request.field_path)),
                }
            )
        return {"verifications": verifications}


def _upload(client, auth_headers, ocr_text: str, label: str):
    return client.post(
        "/api/v1/imports",
        headers=auth_headers,
        files={"files": ("visit.jpg", IMAGE_BYTES, "image/jpeg")},
        data={"mock_ocr_text": ocr_text, "label": label},
    )


# ---------------------------------------------------------------------------
# Pure schema/apply gates
# ---------------------------------------------------------------------------


def _parsed_with_unit_gap() -> ParsedDocument:
    from app.services.ai.extractor import extraction_to_parsed_document

    ocr_text = _vision_ocr()
    extraction = {
        "patient": {"real_name": "测试甲", "sex": "male", "age": 58,
                    "medical_record_number": "0001112223"},
        "encounter": {"hospital": "暨南大学附属第一医院", "department": "中医科",
                      "physician": None, "encounter_date": "2026-08-20",
                      "chief_complaint": None, "present_illness": None,
                      "past_history": None, "allergy": None, "vital_signs": None,
                      "advice": None, "treatment_record": None},
        "symptoms": [],
        "diagnoses": [{"text": "脾虚湿困", "diagnosis_type": "tcm_pattern",
                       "is_primary": True}],
        "prescription": {"total_decoctions": 7, "raw_text": "共7剂",
                         "items": [_payload(ocr_text)["prescription"]["items"][0]]},
        "extraction_warnings": [],
        "confidence": 0.96,
    }
    from app.services.ai.extractor import validate_clinical_extraction

    return extraction_to_parsed_document(
        validate_clinical_extraction(extraction, ocr_text),
        template="AI_UNIFIED",
        parser_version="test",
    )


def test_requests_are_built_only_for_eligible_codes() -> None:
    parsed = _parsed_with_unit_gap()
    requests = build_vision_field_requests(parsed, ["PACKAGE_UNIT_MISSING_IN_OCR"])
    assert [r.kind for r in requests] == ["package_unit"]
    assert requests[0].field_path == "prescription.items[0].original_package_unit"
    # A non-whitelisted code never produces a question.
    assert build_vision_field_requests(parsed, ["PRESCRIPTION_NOT_FOUND"]) == []


def test_unexpected_answer_field_rejects_whole_verification() -> None:
    parsed = _parsed_with_unit_gap()
    requests = build_vision_field_requests(parsed, ["PACKAGE_UNIT_MISSING_IN_OCR"])
    response = parse_vision_verification_response(
        {
            "verifications": [
                {"field_path": "prescription.items[0].original_package_unit",
                 "read_value": "包", "confidence": 0.99, "evidence_quote": "包"},
                {"field_path": "prescription.items[1].raw_drug_name",
                 "read_value": "白芍", "confidence": 0.99, "evidence_quote": None},
            ]
        }
    )
    try:
        apply_vision_verifications(parsed, requests, response)
    except VisionVerificationRejected:
        pass
    else:
        raise AssertionError("answering an unrequested field must fail closed")


def test_unclear_read_stays_missing_and_low_confidence_never_applies() -> None:
    parsed = _parsed_with_unit_gap()
    requests = build_vision_field_requests(parsed, ["PACKAGE_UNIT_MISSING_IN_OCR"])
    response = parse_vision_verification_response(
        {
            "verifications": [
                {"field_path": "prescription.items[0].original_package_unit",
                 "read_value": None, "confidence": 0.99, "evidence_quote": None},
            ]
        }
    )
    patched, records = apply_vision_verifications(parsed, requests, response)
    assert records[0]["applied"] is False
    assert patched.prescription_items[0].original_package_unit is None

    low = parse_vision_verification_response(
        {
            "verifications": [
                {"field_path": "prescription.items[0].original_package_unit",
                 "read_value": "包", "confidence": 0.5, "evidence_quote": "包"},
            ]
        }
    )
    patched_low, records_low = apply_vision_verifications(parsed, requests, low)
    assert records_low[0]["applied"] is False
    assert patched_low.prescription_items[0].original_package_unit is None


def test_masked_or_nonnumeric_identity_is_never_applied() -> None:
    parsed = _parsed_with_unit_gap()
    requests = build_vision_field_requests(parsed, ["MEDICAL_RECORD_NUMBER_MISSING"])
    masked = parse_vision_verification_response(
        {
            "verifications": [
                {"field_path": "patient_identity.medical_record_number",
                 "read_value": "*1234*", "confidence": 0.99, "evidence_quote": None},
            ]
        }
    )
    _, masked_records = apply_vision_verifications(parsed, requests, masked)
    assert masked_records[0]["applied"] is False

    digits = parse_vision_verification_response(
        {
            "verifications": [
                {"field_path": "patient_identity.medical_record_number",
                 "read_value": "000 111 2223", "confidence": 0.99,
                 "evidence_quote": "病历号 000 111 2223"},
            ]
        }
    )
    patched, digit_records = apply_vision_verifications(parsed, requests, digits)
    assert digit_records[0]["applied"] is True
    assert digit_records[0]["source"] == SOURCE_AI_VISUAL_VERIFICATION
    assert patched.medical_record_number == "0001112223"


# ---------------------------------------------------------------------------
# Pipeline integration
# ---------------------------------------------------------------------------


def test_vision_fallback_is_off_by_default(client, auth_headers, monkeypatch, tmp_path) -> None:
    ocr_text = _vision_ocr()
    _use_registry(monkeypatch, tmp_path, ocr_text, _payload(ocr_text))
    # Neither flag is set (configuration defaults): no factory patch needed.

    response = _upload(client, auth_headers, ocr_text, "视觉回退默认关闭")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "UNRESOLVED"
    with SessionLocal() as session:
        ledger = session.scalar(select(UnresolvedIngestion))
        assert ledger is not None
        assert ledger.failure_reason_code == "PACKAGE_UNIT_MISSING_IN_OCR"
        assert session.scalar(select(func.count(VisionVerification.id))) == 0


def test_unit_read_completes_import_with_provenance(
    client, auth_headers, monkeypatch, tmp_path
) -> None:
    ocr_text = _vision_ocr()
    _use_registry(monkeypatch, tmp_path, ocr_text, _payload(ocr_text))
    stub = StubVisionProvider({"prescription.items[0].original_package_unit": "包"})
    _enable_vision(monkeypatch, stub)

    response = _upload(client, auth_headers, ocr_text, "视觉回退补全单位")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "ACCEPTED"

    # Exactly one constrained call, carrying the original image.
    assert len(stub.calls) == 1
    assert stub.calls[0]["image_size"] == len(IMAGE_BYTES)
    assert stub.calls[0]["requested"] == ["prescription.items[0].original_package_unit"]

    with SessionLocal() as session:
        item = session.scalar(
            select(PrescriptionItem).where(PrescriptionItem.raw_drug_name == "茯神")
        )
        assert item is not None
        assert item.original_package_unit == "包"
        assert item.normalized_package_unit == "package"
        assert item.calculated_dose_g == 15
        assert session.scalar(select(func.count(Encounter.id))) == 1

        record = session.scalar(select(VisionVerification))
        assert record is not None
        assert record.status == "applied"
        assert record.source == SOURCE_AI_VISUAL_VERIFICATION
        assert record.image_sha256 == hashlib.sha256(IMAGE_BYTES).hexdigest()
        assert record.issue_codes == ["PACKAGE_UNIT_MISSING_IN_OCR"]
        assert record.applied_fields
        document = session.get(Document, record.document_id)
        assert document is not None
        snapshot = document.structured_snapshot
        assert snapshot["vision_verifications"]
        assert snapshot["vision_verifications"][0]["source"] == SOURCE_AI_VISUAL_VERIFICATION


def test_unclear_read_stays_unresolved(client, auth_headers, monkeypatch, tmp_path) -> None:
    ocr_text = _vision_ocr()
    _use_registry(monkeypatch, tmp_path, ocr_text, _payload(ocr_text))
    stub = StubVisionProvider({"prescription.items[0].original_package_unit": None})
    _enable_vision(monkeypatch, stub)

    response = _upload(client, auth_headers, ocr_text, "视觉仍不明确")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "UNRESOLVED"

    with SessionLocal() as session:
        record = session.scalar(select(VisionVerification))
        assert record is not None
        assert record.status == "unresolvable"
        assert session.scalar(select(func.count(Encounter.id))) == 0
        ledger = session.scalar(select(UnresolvedIngestion))
        assert ledger is not None
        assert ledger.failure_reason_code == "PACKAGE_UNIT_MISSING_IN_OCR"


def test_contract_violation_fails_closed(client, auth_headers, monkeypatch, tmp_path) -> None:
    ocr_text = _vision_ocr()
    _use_registry(monkeypatch, tmp_path, ocr_text, _payload(ocr_text))

    class RogueProvider(StubVisionProvider):
        def verify_fields(self, image_bytes, media_type, requests):
            super().verify_fields(image_bytes, media_type, requests)
            return {
                "verifications": [
                    {"field_path": "patient.real_name", "read_value": "测试乙",
                     "confidence": 0.99, "evidence_quote": None}
                ]
            }

    _enable_vision(monkeypatch, RogueProvider({}))
    response = _upload(client, auth_headers, ocr_text, "越权回答")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "UNRESOLVED"

    with SessionLocal() as session:
        record = session.scalar(select(VisionVerification))
        assert record is not None
        assert record.status == "rejected"
        assert session.scalar(select(func.count(Patient.id))) == 0


def test_mrn_read_enters_deterministic_matching(
    client, auth_headers, monkeypatch, tmp_path
) -> None:
    ocr_text = _vision_ocr(with_mrn=False)
    _use_registry(
        monkeypatch, tmp_path, ocr_text, _units_complete(_payload(ocr_text, with_mrn=False))
    )
    stub = StubVisionProvider({"patient_identity.medical_record_number": "0001112223"})
    _enable_vision(monkeypatch, stub)

    response = _upload(client, auth_headers, ocr_text, "视觉读取病历号")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "ACCEPTED"

    with SessionLocal() as session:
        # The vision-read MRN entered the deterministic matcher: one
        # patient/encounter pair was committed for the document.
        assert session.scalar(select(func.count(Encounter.id))) == 1
        record = session.scalar(select(VisionVerification))
        assert record is not None
        assert record.status == "applied"
        assert record.applied_fields[0]["field_path"] == (
            "patient_identity.medical_record_number"
        )


def test_non_eligible_blocking_code_never_calls_vision(
    client, auth_headers, monkeypatch, tmp_path
) -> None:
    ocr_text = _vision_ocr()
    payload = _units_complete(_payload(ocr_text, with_date=False))
    _use_registry(monkeypatch, tmp_path, ocr_text, payload)
    stub = StubVisionProvider({})
    _enable_vision(monkeypatch, stub)

    response = _upload(client, auth_headers, ocr_text, "非白名单代码不触发视觉")
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "UNRESOLVED"
    assert stub.calls == []
    with SessionLocal() as session:
        assert session.scalar(select(func.count(VisionVerification.id))) == 0
        ledger = session.scalar(select(UnresolvedIngestion))
        assert ledger is not None
        assert ledger.failure_reason_code == "ENCOUNTER_DATE_MISSING"
