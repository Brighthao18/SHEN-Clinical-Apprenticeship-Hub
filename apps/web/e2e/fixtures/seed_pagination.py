"""Seed 205 synthetic works on one patient for the works-list pagination E2E.

Run from apps/api (runner sets DATABASE_URL to shen_e2e):
  python - < ../web/e2e/fixtures/seed_pagination.py

Idempotent. Patient E2E-PAGE-01 with two encounters; works numbered
001..205 (user_final, one version each, created chronologically) so the
latest-sorted first page shows 205.. and the last page shows 001..005.
Emits E2E_SEED_JSON with patient/encounter ids.
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone

CODE = "E2E-PAGE-01"
COUNT = 205
BASE = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)


def _document(label: str):
    from app.models.domain import Document
    from app.models.enums import DocumentType, ProcessingStatus

    digest = hashlib.sha256(label.encode()).hexdigest()
    return Document(
        id=uuid.uuid4(),
        original_filename=f"{label}.jpg",
        original_file_path=f"synthetic/{digest}.jpg",
        sha256=digest,
        byte_size=10,
        mime_type="image/jpeg",
        document_type=DocumentType.OUTPATIENT_RECORD,
        processing_status=ProcessingStatus.ACCEPTED,
    )


def main() -> int:
    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.domain import (
        ApprenticeshipNoteVersion,
        Diagnosis,
        Document,
        Encounter,
        Patient,
        PatientIdentity,
    )
    from app.models.enums import DiagnosisType, NoteVersionType, Sex, SourceType

    with SessionLocal() as session:
        patient = session.scalar(select(Patient).where(Patient.internal_patient_code == CODE))
        if patient is None:
            patient = Patient(internal_patient_code=CODE, sex=Sex.FEMALE)
            patient.identities.append(
                PatientIdentity(
                    hospital="E2E合成医院",
                    real_name="分页验收",
                    hospital_medical_record_number="E2E-2026-003",
                )
            )
            encounters = []
            for index, (when, complaint) in enumerate(
                (
                    (BASE, "分页验收就诊一"),
                    (BASE + timedelta(days=30), "分页验收就诊二"),
                ),
                start=1,
            ):
                document = _document(f"e2e-page-enc-{index}")
                encounter = Encounter(
                    patient=patient,
                    primary_document=document,
                    primary_document_id=document.id,
                    hospital="E2E合成医院",
                    encounter_at=when,
                    chief_complaint_raw=complaint,
                    raw_source_type=SourceType.PHYSICIAN_RECORD,
                )
                encounter.diagnoses.append(
                    Diagnosis(
                        diagnosis_type=DiagnosisType.TCM_PATTERN,
                        name_raw="痰湿蕴肺",
                        name_normalized="痰湿蕴肺",
                        is_primary=True,
                        source_type=SourceType.PHYSICIAN_RECORD,
                        evidence_document_id=document.id,
                    )
                )
                session.add(document)
                encounters.append(encounter)
            session.add(patient)
            session.flush()

            rows = []
            for index in range(1, COUNT + 1):
                rows.append(
                    ApprenticeshipNoteVersion(
                        note_id=uuid.uuid4(),
                        patient_id=patient.id,
                        note_type=__import__(
                            "app.models.enums", fromlist=["ApprenticeshipNoteType"]
                        ).ApprenticeshipNoteType.CASE_RECORD,
                        version=1,
                        version_type=NoteVersionType.USER_FINAL,
                        title=f"分页验收作品 {index:03d}",
                        content=f"分页验收作品 {index:03d}（合成数据）。",
                        evidence_encounter_ids=[str(encounters[0].id)],
                        source_context_hash=hashlib.sha256(f"page-{index}".encode()).hexdigest(),
                        deidentification_version="e2e-seed-v1",
                        created_at=BASE + timedelta(hours=index),
                    )
                )
            for row in rows:
                session.add(row)
            session.commit()

        patient = session.scalar(select(Patient).where(Patient.internal_patient_code == CODE))
        encounters = sorted(patient.encounters, key=lambda item: item.encounter_at)
        print(
            "E2E_SEED_JSON:"
            + json.dumps(
                {
                    "patient_id": str(patient.id),
                    "encounter_older_id": str(encounters[0].id),
                    "encounter_newer_id": str(encounters[1].id),
                    "works": COUNT,
                },
                separators=(",", ":"),
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
