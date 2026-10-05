"""Seed the isolated shen_e2e database for the repeatable browser E2E suite.

Run from apps/api with DATABASE_URL pointing at shen_e2e (the runner does this):
  cd apps/api && DATABASE_URL=... python - < ../web/e2e/fixtures/seed_dashboard.py <phase>

Phases (idempotent):
  base  patient E2E-DASH-01 with TWO encounters + 20 finalized notes
  draft the OLDER still-in-progress draft note (F1 regression data)

Synthetic data only. Emits one machine-readable line:
  E2E_SEED_JSON:{"patient_id": "...", "encounter_older_id": "...", ...}
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone

CODE = "E2E-DASH-01"
OLDER_AT = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)
NEWER_AT = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
DRAFT_AT = datetime(2026, 8, 20, 10, 0, tzinfo=timezone.utc)
DRAFT_TITLE = "较早的未完成作品（应显示在继续工作）"
FINAL_COUNT = 20
DEIDENT_VERSION = "e2e-seed-v1"


def _document(label: str):
    from app.models.domain import Document
    from app.models.enums import DocumentType, ProcessingStatus

    digest = hashlib.sha256(label.encode()).hexdigest()
    return Document(
        id=__import__("uuid").uuid4(),
        original_filename=f"{label}.jpg",
        original_file_path=f"synthetic/{digest}.jpg",
        sha256=digest,
        byte_size=10,
        mime_type="image/jpeg",
        document_type=DocumentType.OUTPATIENT_RECORD,
        processing_status=ProcessingStatus.ACCEPTED,
    )


def main() -> int:
    import uuid

    from sqlalchemy import select

    from app.db.session import SessionLocal
    from app.models.domain import (
        ApprenticeshipNoteVersion,
        Diagnosis,
        Document,
        Encounter,
        Patient,
        PatientIdentity,
        Prescription,
        PrescriptionItem,
        SymptomObservation,
    )
    from app.models.enums import (
        ApprenticeshipNoteType,
        DiagnosisType,
        DoseScope,
        DoseSource,
        NoteKind,
        NoteVersionType,
        Sex,
        SourceType,
        SymptomStatus,
        SymptomTrend,
    )

    phase = sys.argv[1] if len(sys.argv) > 1 else "base"
    if phase not in {"base", "draft"}:
        print(f"unknown phase: {phase}", file=sys.stderr)
        return 2

    with SessionLocal() as session:
        patient = session.scalar(select(Patient).where(Patient.internal_patient_code == CODE))
        if patient is None:
            patient = Patient(internal_patient_code=CODE, sex=Sex.FEMALE)
            patient.identities.append(
                PatientIdentity(
                    hospital="E2E合成医院",
                    real_name="工作台验收",
                    hospital_medical_record_number="E2E-2026-002",
                )
            )
            encounter_specs = [
                ("e2e-dash-enc-older", OLDER_AT, "咳嗽加重，痰白清稀"),
                ("e2e-dash-enc-newer", NEWER_AT, "咳嗽好转，仍晨起少量白痰"),
            ]
            encounters = []
            for label, when, complaint in encounter_specs:
                document = _document(label)
                encounter = Encounter(
                    patient=patient,
                    primary_document=document,
                    primary_document_id=document.id,
                    hospital="E2E合成医院",
                    encounter_at=when,
                    chief_complaint_raw=complaint,
                    raw_source_type=SourceType.PHYSICIAN_RECORD,
                )
                encounter.symptoms.append(
                    SymptomObservation(
                        symptom_raw="咳嗽",
                        symptom_normalized="咳嗽",
                        status=SymptomStatus.PRESENT,
                        trend=SymptomTrend.PERSISTENT,
                        source_type=SourceType.PHYSICIAN_RECORD,
                        evidence_document_id=document.id,
                    )
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
                prescription = Prescription(
                    encounter=encounter,
                    evidence_document_id=document.id,
                    total_decoctions=7,
                    source_type=SourceType.PHYSICIAN_RECORD,
                )
                for line_number, drug, dose in ((1, "陈皮", "6"), (2, "茯苓", "10")):
                    prescription.items.append(
                        PrescriptionItem(
                            line_number=line_number,
                            original_text=f"{drug} {dose}g",
                            raw_drug_name=drug,
                            canonical_drug_name=drug,
                            calculated_dose_g=__import__("decimal").Decimal(dose),
                            dose_source=DoseSource.CALCULATED,
                            dose_scope=DoseScope.PER_DECOCTION,
                            normalization_status="normalized",
                            parser_version="e2e-dash-v1",
                        )
                    )
                session.add(document)
                encounters.append(encounter)
            session.add(patient)
            session.flush()
            older, newer = encounters

            finals = [
                ApprenticeshipNoteVersion(
                    note_id=uuid.uuid4(),
                    patient_id=patient.id,
                    note_type=ApprenticeshipNoteType.CASE_RECORD,
                    version=1,
                    version_type=NoteVersionType.USER_FINAL,
                    title=f"已定稿作品 {index:02d}",
                    content=f"这是第 {index} 篇已定稿医案（合成数据），仅用于把较早草稿挤出前 20 条。",
                    evidence_encounter_ids=[str(older.id)],
                    source_context_hash=hashlib.sha256(f"final-{index}".encode()).hexdigest(),
                    deidentification_version=DEIDENT_VERSION,
                    created_at=datetime(2026, 9, 2, 1 + index, 0, tzinfo=timezone.utc),
                )
                for index in range(1, FINAL_COUNT + 1)
            ]
            # 跟师记录：学生补录会改变写作材料（依据预览 409 场景的数据面）。
            older.student_notes.append(
                StudentNoteForSeed(patient, older, "老师口头记录：痰湿咳嗽，二陈汤加减。")
            )
            for version in finals:
                session.add(version)
            session.commit()

        patient = session.scalar(select(Patient).where(Patient.internal_patient_code == CODE))
        encounters = sorted(patient.encounters, key=lambda item: item.encounter_at)
        older, newer = encounters

        draft_note_id = None
        if phase == "draft":
            existing_draft = session.scalar(
                select(ApprenticeshipNoteVersion).where(
                    ApprenticeshipNoteVersion.title == DRAFT_TITLE
                )
            )
            if existing_draft is None:
                draft = ApprenticeshipNoteVersion(
                    note_id=uuid.uuid4(),
                    patient_id=patient.id,
                    note_type=ApprenticeshipNoteType.CASE_RECORD,
                    version=1,
                    version_type=NoteVersionType.USER_DRAFT,
                    title=DRAFT_TITLE,
                    content="这是 2026-08-20 起草、尚未定稿的医案整理草稿，用于验证较早的进行中作品不被最近的定稿挤出。",
                    evidence_encounter_ids=[str(older.id)],
                    source_context_hash=hashlib.sha256(b"draft").hexdigest(),
                    deidentification_version=DEIDENT_VERSION,
                    created_at=DRAFT_AT,
                )
                session.add(draft)
                session.commit()
                draft_note_id = str(draft.note_id)
            else:
                draft_note_id = str(existing_draft.note_id)

        print(
            "E2E_SEED_JSON:"
            + json.dumps(
                {
                    "phase": phase,
                    "patient_id": str(patient.id),
                    "encounter_older_id": str(older.id),
                    "encounter_newer_id": str(newer.id),
                    "draft_note_id": draft_note_id,
                },
                ensure_ascii=False,
            )
        )
    return 0


def StudentNoteForSeed(patient, encounter, content: str):
    from app.models.domain import StudentNote
    from app.models.enums import NoteKind

    return StudentNote(patient=patient, note_kind=NoteKind.TEACHER_ORAL, content=content)


if __name__ == "__main__":
    sys.exit(main())
