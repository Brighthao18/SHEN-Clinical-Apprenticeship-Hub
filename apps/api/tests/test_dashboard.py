"""Dashboard aggregation tests: weekly counters, patient cohorts, recent rows."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.models.enums import ProcessingStatus

FIXTURE = Path(__file__).parent / "fixtures" / "jnu_outpatient_v1.txt"


def test_empty_dashboard_returns_zeroed_counts(client: TestClient, auth_headers) -> None:
    response = client.get("/api/v1/dashboard", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["counts"] == {
        "imported": 0,
        "accepted": 0,
        "unresolved": 0,
        "compatibility_reviews": 0,
        "new_patients": 0,
        "returning_patients": 0,
    }
    assert payload["recent_patients"] == []
    assert payload["recent_encounters"] == []
    assert payload["recent_analyses"] == []
    assert payload["recent_notes"] == []
    assert payload["ocr_provider"] == "mock"
    assert payload["timezone"]
    assert payload["week_start"]


def test_dashboard_counts_after_one_import_and_analysis(
    client: TestClient, auth_headers
) -> None:
    upload = client.post(
        "/api/v1/imports",
        headers=auth_headers,
        files={"files": ("visit.jpg", b"dashboard-fixture-bytes", "image/jpeg")},
        data={"mock_ocr_text": FIXTURE.read_text(encoding="utf-8"), "label": "仪表盘周"},
    )
    assert upload.status_code == 202, upload.text
    item = upload.json()["items"][0]

    analysis = client.post(
        f"/api/v1/encounters/{item['encounter_id']}/analyses",
        headers=auth_headers,
        json={"task_type": "encounter_interpretation"},
    )
    assert analysis.status_code == 200, analysis.text

    response = client.get("/api/v1/dashboard", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    counts = payload["counts"]
    assert counts["imported"] == 1
    assert counts["accepted"] == 1
    assert counts["unresolved"] == 0
    # The fixture records two non-blocking hospital-speculation-gap reviews.
    assert counts["compatibility_reviews"] == 2
    # The fixture's printed encounter date (2026-08-14) is intentionally in
    # an earlier ISO week; weekly patient cohorts are driven by encounter
    # dates, not upload time, and are covered by the seeded cohort test below.
    assert counts["new_patients"] == 0
    assert counts["returning_patients"] == 0

    assert len(payload["recent_patients"]) == 1
    assert payload["recent_patients"][0]["code"] == "P000001"
    assert len(payload["recent_encounters"]) == 1
    assert payload["recent_encounters"][0]["chief_complaint"].startswith("腹痛")
    assert len(payload["recent_analyses"]) == 1
    assert payload["recent_analyses"][0]["task_type"] == "encounter_interpretation"


def test_weekly_patient_cohorts_use_encounter_dates(
    client: TestClient, auth_headers, db_session
) -> None:
    """New vs returning patients are counted by encounter dates inside the
    local-time ISO week, never by upload time."""

    from datetime import UTC, datetime, time, timedelta
    from zoneinfo import ZoneInfo

    from app.models.domain import Document, Encounter, Patient
    from app.models.enums import DocumentType, Sex

    settings = get_settings()
    zone = ZoneInfo(settings.app_timezone)
    now_local = datetime.now(zone)
    now = now_local.astimezone(UTC)
    week_start_local = datetime.combine(
        (now_local - timedelta(days=now_local.weekday())).date(), time.min, tzinfo=zone
    )
    before_week = (week_start_local - timedelta(days=3)).astimezone(UTC)

    new_patient = Patient(internal_patient_code="D000001", sex=Sex.FEMALE)
    returning_patient = Patient(internal_patient_code="D000002", sex=Sex.MALE)
    db_session.add_all([new_patient, returning_patient])
    db_session.flush()
    new_patient.first_encounter_at = now
    new_patient.last_encounter_at = now
    returning_patient.first_encounter_at = before_week
    returning_patient.last_encounter_at = now
    docs = []
    for index, _patient in enumerate((new_patient, returning_patient), start=1):
        digest = hashlib.sha256(f"cohort-{index}".encode()).hexdigest()
        docs.append(
            Document(
                id=uuid.uuid4(),
                original_filename=f"cohort-{index}.jpg",
                original_file_path=f"synthetic/{digest}.jpg",
                sha256=digest,
                byte_size=10,
                mime_type="image/jpeg",
                document_type=DocumentType.OUTPATIENT_RECORD,
                processing_status=ProcessingStatus.ACCEPTED,
            )
        )
    db_session.add_all(docs)
    db_session.flush()
    db_session.add_all(
        [
            Encounter(
                patient_id=patient.id,
                primary_document_id=document.id,
                hospital="合成测试医院",
                encounter_at=encounter_time,
            )
            for patient, document, encounter_time in (
                (new_patient, docs[0], now),
                (returning_patient, docs[1], now),
            )
        ]
    )
    db_session.commit()

    response = client.get("/api/v1/dashboard", headers=auth_headers)
    assert response.status_code == 200, response.text
    counts = response.json()["counts"]
    assert counts["new_patients"] == 1
    assert counts["returning_patients"] == 1
