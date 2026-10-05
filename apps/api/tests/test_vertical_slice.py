from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models.domain import (
    AuditEvent,
    Document,
    Encounter,
    OCRProviderSubmission,
    Patient,
    PrescriptionItem,
    ProcessingJob,
    UnresolvedIngestion,
)
from app.models.enums import JobStatus, ProcessingStatus
from app.services.reprocess import reprocess_unresolved
from app.services.validation_policy import VALIDATION_POLICY_VERSION

FIXTURE = Path(__file__).parent / "fixtures" / "jnu_outpatient_v1.txt"


def test_upload_to_patient_timeline_is_idempotent(client, auth_headers) -> None:
    ocr_text = FIXTURE.read_text(encoding="utf-8")
    upload = {
        "files": ("visit.jpg", b"fake-jpeg-source-evidence", "image/jpeg"),
    }
    response = client.post(
        "/api/v1/imports",
        headers=auth_headers,
        files=upload,
        data={"mock_ocr_text": ocr_text, "label": "本周导入"},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    # Phase 5.2: reference-data gaps (unknown hospital specs) are recorded as
    # non-blocking warnings; only issues that could pollute critical clinical
    # fields may block auto-accept.
    assert payload["status"] == "ACCEPTED"
    assert payload["review_items"] == 0
    assert payload["unresolved_items"] == 0
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["patient_id"]
    assert item["encounter_id"]
    document_id = item["document_id"]

    patient_response = client.get(f"/api/v1/patients/{item['patient_id']}", headers=auth_headers)
    assert patient_response.status_code == 200, patient_response.text
    patient = patient_response.json()
    assert patient["internal_patient_code"] == "P000001"
    assert patient["display_name"] == "张某"
    encounter = patient["encounters"][0]
    assert encounter["present_illness_raw"] is None
    assert encounter["chief_complaint_raw"].startswith("腹痛")
    prescription_items = encounter["prescriptions"][0]["items"]
    assert len(prescription_items) == 11
    # Item provenance mirrors the prescription's real source; this fixture is
    # structured by the mock AI extraction provider, so both are ai_normalized.
    prescription_source = encounter["prescriptions"][0]["source"]
    assert prescription_source == "ai_normalized"
    assert all(item["source"] == prescription_source for item in prescription_items)
    vinegar_shell = next(i for i in prescription_items if i["raw_drug_name"] == "醋鳖甲")
    assert vinegar_shell["canonical_drug_name"] == "鳖甲"
    assert vinegar_shell["calculated_dose_g"] == "20"
    assert vinegar_shell["dose_scope"] == "per_decoction"
    rhodiola = next(i for i in prescription_items if i["raw_drug_name"] == "红景天")
    assert rhodiola["calculated_dose_g"] is None

    content = client.get(f"/api/v1/documents/{document_id}/content", headers=auth_headers)
    assert content.status_code == 200
    assert content.content == b"fake-jpeg-source-evidence"

    review_response = client.get("/api/v1/reviews?status=open", headers=auth_headers)
    assert review_response.status_code == 200
    assert len(review_response.json()) == 2
    assert {review["issue_code"] for review in review_response.json()} == {
        "HOSPITAL_SPECIFICATION_UNKNOWN"
    }
    assert all(review["blocking"] is False for review in review_response.json())

    duplicate = client.post(
        "/api/v1/imports",
        headers=auth_headers,
        files=upload,
        data={"mock_ocr_text": ocr_text, "label": "重复上传验证"},
    )
    assert duplicate.status_code == 202
    assert duplicate.json()["items"][0]["document_id"] == document_id

    # Recent-batches listing: light summaries without items, newest first.
    recent = client.get("/api/v1/imports?limit=5", headers=auth_headers)
    assert recent.status_code == 200, recent.text
    recent_batches = recent.json()
    assert payload["id"] in {batch["id"] for batch in recent_batches}
    assert all("items" not in batch for batch in recent_batches)

    with SessionLocal() as session:
        assert session.scalar(select(func.count(Patient.id))) == 1
        assert session.scalar(select(func.count(Encounter.id))) == 1
        assert session.scalar(select(func.count(Document.id))) == 1
        assert session.scalar(select(func.count(PrescriptionItem.id))) == 11
        assert session.scalar(select(func.count(OCRProviderSubmission.id))) == 1
        submission = session.scalar(select(OCRProviderSubmission))
        assert submission is not None
        assert submission.provider == "mock"
        assert submission.status == "completed"
        assert submission.completed_at is not None

    analysis = client.post(
        f"/api/v1/encounters/{item['encounter_id']}/analyses",
        headers=auth_headers,
        json={"task_type": "encounter_interpretation"},
    )
    assert analysis.status_code == 200, analysis.text
    assert analysis.json()["provider"] == "mock"
    assert analysis.json()["source_documents"] == [document_id]


def test_unknown_template_routes_to_unresolved_without_clinical_rows(client, auth_headers) -> None:
    upload = {
        "files": ("receipt.jpg", b"non-clinical-receipt", "image/jpeg"),
    }
    response = client.post(
        "/api/v1/imports",
        headers=auth_headers,
        files=upload,
        data={"mock_ocr_text": "普通购物小票\n合计 42 元\n谢谢惠顾", "label": "非临床文档"},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "UNRESOLVED"
    assert payload["unresolved_items"] == 1
    assert payload["items"][0]["patient_id"] is None
    assert payload["items"][0]["encounter_id"] is None

    with SessionLocal() as session:
        assert session.scalar(select(func.count(Patient.id))) == 0
        assert session.scalar(select(func.count(Encounter.id))) == 0
        ledger = session.scalar(select(UnresolvedIngestion))
        assert ledger is not None
        assert ledger.template == "UNKNOWN"
        # Phase 6: the mock extractor fails closed when no fixture matches
        # the OCR text; a non-clinical document never reaches clinical rows.
        assert ledger.failure_reason_code == "AI_EXTRACTION_PROVIDER_FAILED"
        assert ledger.validation_policy_version == VALIDATION_POLICY_VERSION
        assert ledger.retry_count == 1
        unresolved_audit = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "pipeline.unresolved")
        )
        assert unresolved_audit is not None
        assert unresolved_audit.after_redacted is not None
        assert (
            unresolved_audit.after_redacted["validation_policy_version"]
            == VALIDATION_POLICY_VERSION
        )

    queue = client.get("/api/v1/ingestion/unresolved", headers=auth_headers)
    assert queue.status_code == 200, queue.text
    rows = queue.json()
    assert len(rows) == 1
    assert rows[0]["failure_reason_code"] == "AI_EXTRACTION_PROVIDER_FAILED"
    assert rows[0]["template"] == "UNKNOWN"

    compatibility_reviews = client.get(
        "/api/v1/reviews?status=open", headers=auth_headers
    )
    assert compatibility_reviews.status_code == 200
    assert compatibility_reviews.json() == []

    # Explicit reprocess is a dry run unless --apply semantics are requested.
    with SessionLocal() as session:
        dry = reprocess_unresolved(session, apply=False)
        assert dry["status"] == "reprocess_dry_run"
        assert dry["planned_document_count"] == 1
        replay = reprocess_unresolved(session, apply=True)
        assert replay["outcomes"] == {"UNRESOLVED": 1}
        ledger = session.scalar(select(UnresolvedIngestion))
        assert ledger is not None
        assert ledger.retry_count == 2
        assert session.scalar(select(func.count(Encounter.id))) == 0


def test_retry_marks_failed_when_dispatch_broker_unreachable(client, auth_headers) -> None:
    ocr_text = (
        "暨南大学附属第一医院\n姓名：测试甲\n病历号：JNU-900001\n日期：2026-08-01\n"
        "处方：\n（基）茯神（10g）2包\n共7剂\n医师：沈老师"
    )
    upload = {"files": ("retry.jpg", b"retry-image-bytes", "image/jpeg")}
    response = client.post(
        "/api/v1/imports",
        headers=auth_headers,
        files=upload,
        data={"mock_ocr_text": ocr_text, "label": "重试失败路径"},
    )
    assert response.status_code == 202, response.text
    document_id = response.json()["items"][0]["document_id"]

    with SessionLocal() as session:
        document = session.get(Document, uuid.UUID(document_id))
        assert document is not None
        document.processing_status = ProcessingStatus.FAILED
        document.failure_code = "OCR_PROVIDER_TIMEOUT"
        session.commit()

    with patch(
        "app.api.routes.documents.dispatch_document", side_effect=RuntimeError("broker down")
    ):
        retry = client.post(f"/api/v1/documents/{document_id}/retry", headers=auth_headers)
    assert retry.status_code == 503
    assert "任务队列" in retry.json()["detail"]

    with SessionLocal() as session:
        document = session.get(Document, uuid.UUID(document_id))
        assert document is not None
        assert document.processing_status == ProcessingStatus.FAILED
        assert document.failure_code == "DISPATCH_FAILED"
        job = session.scalar(select(ProcessingJob).where(ProcessingJob.document_id == document.id))
        assert job is not None
        assert job.status == JobStatus.FAILED
        assert job.error_code == "DISPATCH_FAILED"

    # A later retry once the broker is healthy returns to the queued state.
    retried = client.post(f"/api/v1/documents/{document_id}/retry", headers=auth_headers)
    assert retried.status_code == 200
    assert retried.json()["status"] == "QUEUED"
