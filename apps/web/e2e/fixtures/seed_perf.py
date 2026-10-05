"""Seed performance-scale synthetic data into the isolated shen_e2e database.

Run from apps/api (runner sets DATABASE_URL to shen_e2e):
  python - < ../web/e2e/fixtures/seed_perf.py notes <count>     # count notes x 5 versions
  python - < ../web/e2e/fixtures/seed_perf.py encounters <count> # count encounters x 1 note

Recreate the database per scale for clean numbers. Bulk inserts use Core
executemany; evidence snapshots get a deterministic size distribution
(roughly 0.5-4 KB, never empty) and the seed prints the size stats.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

BASE = datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)
DEIDENT = "perf-seed-v1"


def snapshot_bytes(note_index: int, version: int) -> str:
    # Deterministic 0.5-4KB pseudo-material: never empty, varied sizes.
    size = 400 + ((note_index * 977 + version * 331) % 3600)
    filler = ("证" * 4 + "，") * (size // 10)
    return json.dumps(
        {
            "context_text": filler,
            "note_index": note_index,
            "version": version,
            "hash": hashlib.sha256(f"{note_index}-{version}".encode()).hexdigest(),
        },
        ensure_ascii=False,
    )


def main() -> int:
    import uuid

    from sqlalchemy import insert, select

    from app.db.session import SessionLocal
    from app.models.domain import (
        ApprenticeshipNoteVersion,
        Diagnosis,
        Document,
        Encounter,
        Patient,
        PatientIdentity,
    )
    from app.models.enums import (
        ApprenticeshipNoteType,
        DiagnosisType,
        NoteVersionType,
        Sex,
        SourceType,
    )

    mode = sys.argv[1] if len(sys.argv) > 1 else "notes"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 100

    with SessionLocal() as session:
        if mode == "notes":
            code = f"PERF-NOTES-{count}"
        else:
            code = f"PERF-ENC-{count}"
        patient = session.scalar(select(Patient).where(Patient.internal_patient_code == code))
        if patient is not None:
            print(f"E2E_SEED_JSON:{{\"code\": \"{code}\", \"already\": true}}")
            return 0

        patient = Patient(internal_patient_code=code, sex=Sex.FEMALE)
        patient.identities.append(
            PatientIdentity(
                hospital="E2E合成医院",
                real_name="性能基线",
                hospital_medical_record_number=f"PERF-{count}",
            )
        )

        if mode == "notes":
            encounter_count = 2
        else:
            encounter_count = count
        documents = []
        encounters = []
        for index in range(1, encounter_count + 1):
            digest = hashlib.sha256(f"perf-{code}-{index}".encode()).hexdigest()
            document = Document(
                id=uuid.uuid4(),
                original_filename=f"perf-{index}.jpg",
                original_file_path=f"synthetic/{digest}.jpg",
                sha256=digest,
                byte_size=10,
                mime_type="image/jpeg",
                document_type=__import__("app.models.enums", fromlist=["DocumentType"]).DocumentType.OUTPATIENT_RECORD,
                processing_status=__import__("app.models.enums", fromlist=["ProcessingStatus"]).ProcessingStatus.ACCEPTED,
            )
            encounter = Encounter(
                patient=patient,
                primary_document=document,
                primary_document_id=document.id,
                hospital="E2E合成医院",
                encounter_at=BASE + timedelta(days=index),
                chief_complaint_raw=f"性能基线就诊 {index}",
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
            documents.append(document)
            encounters.append(encounter)
        session.add_all(documents)
        session.add(patient)
        session.flush()

        version_rows = []
        if mode == "notes":
            for index in range(1, count + 1):
                note_id = uuid.uuid4()
                for version in range(1, 6):
                    created = BASE + timedelta(hours=index * 5 + version)
                    version_rows.append(
                        {
                            "id": uuid.uuid4(),
                            "note_id": note_id,
                            "patient_id": patient.id,
                            "note_type": ApprenticeshipNoteType.CASE_RECORD,
                            "version": version,
                            "version_type": NoteVersionType.USER_FINAL
                            if version == 5
                            else NoteVersionType.USER_DRAFT,
                            "title": f"性能作品 {index:06d}",
                            "content": f"性能基线作品 {index} 版本 {version}（合成数据）。",
                            "evidence_encounter_ids": [str(encounters[0].id)],
                            "source_context_hash": hashlib.sha256(
                                f"perf-{index}".encode()
                            ).hexdigest(),
                            "deidentification_version": DEIDENT,
                            "evidence_snapshot": json.loads(snapshot_bytes(index, version)),
                            "created_at": created,
                        }
                    )
        else:
            for index, encounter in enumerate(encounters, start=1):
                version_rows.append(
                    {
                        "id": uuid.uuid4(),
                        "note_id": uuid.uuid4(),
                        "patient_id": patient.id,
                        "note_type": ApprenticeshipNoteType.CASE_RECORD
                        if index % 2
                        else ApprenticeshipNoteType.TEACHER_NOTE,
                        "version": 1,
                        "version_type": NoteVersionType.USER_FINAL
                        if index % 2
                        else NoteVersionType.USER_DRAFT,
                        "title": f"就诊作品 {index:04d}",
                        "content": f"性能基线就诊 {index} 的作品（合成数据）。",
                        "evidence_encounter_ids": [str(encounter.id)],
                        "source_context_hash": hashlib.sha256(
                            f"perf-enc-{index}".encode()
                        ).hexdigest(),
                        "deidentification_version": DEIDENT,
                        "evidence_snapshot": json.loads(snapshot_bytes(index, 1)),
                        "created_at": BASE + timedelta(days=index, hours=2),
                    }
                )

        session.execute(insert(ApprenticeshipNoteVersion), version_rows)
        session.commit()

        sizes = [len(json.dumps(row["evidence_snapshot"], ensure_ascii=False)) for row in version_rows if "evidence_snapshot" in row]
        print(
            "E2E_SEED_JSON:"
            + json.dumps(
                {
                    "code": code,
                    "patient_id": str(patient.id),
                    "mode": mode,
                    "count": count,
                    "version_rows": len(version_rows),
                    "snapshot_bytes_min": min(sizes),
                    "snapshot_bytes_max": max(sizes),
                    "first_note_id": str(version_rows[0]["note_id"]),
                },
                separators=(",", ":"),
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
