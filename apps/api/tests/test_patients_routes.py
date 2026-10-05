"""Patients route tests: search, pagination, 404, student notes, masking."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.db.session import SessionLocal
from app.db.types import pii_fingerprint
from app.models.domain import (
    Document,
    Encounter,
    Patient,
    PatientIdentity,
    Prescription,
    PrescriptionItem,
    StudentNote,
)
from app.models.enums import DocumentType as DocumentTypeEnum
from app.models.enums import NoteKind, ProcessingStatus, Sex
from app.models.enums import SourceType as SourceTypeEnum


def _seed_patient(
    db_session,
    code: str,
    real_name: str | None = None,
    *,
    with_encounter: bool = False,
) -> Patient:
    patient = Patient(internal_patient_code=code, sex=Sex.FEMALE)
    db_session.add(patient)
    db_session.flush()
    if real_name is not None:
        patient.identities.append(
            PatientIdentity(
                hospital="暨南大学附属第一医院",
                real_name=real_name,
                real_name_fingerprint=pii_fingerprint(real_name),
            )
        )
    if with_encounter:
        digest = hashlib.sha256(code.encode()).hexdigest()
        document = Document(
            id=uuid.uuid4(),
            original_filename=f"{code}.jpg",
            original_file_path=f"synthetic/{digest}.jpg",
            sha256=digest,
            byte_size=10,
            mime_type="image/jpeg",
            document_type=DocumentTypeEnum.OUTPATIENT_RECORD,
            processing_status=ProcessingStatus.ACCEPTED,
        )
        db_session.add(document)
        db_session.flush()
        encounter = Encounter(
            patient_id=patient.id,
            primary_document_id=document.id,
            hospital="暨南大学附属第一医院",
            encounter_at=datetime(2026, 8, 14, tzinfo=UTC),
            chief_complaint_raw="腹痛一周",
        )
        db_session.add(encounter)
        patient.first_encounter_at = encounter.encounter_at
        patient.last_encounter_at = encounter.encounter_at
    db_session.commit()
    return patient


def test_list_patients_searches_by_code_and_real_name(
    client: TestClient, auth_headers, db_session
) -> None:
    """The directory searches the internal code by prefix and the real name
    by PII fingerprint; display stays masked."""

    seeded = _seed_patient(db_session, "P000001", "张三", with_encounter=True)

    by_code = client.get("/api/v1/patients?query=P0000", headers=auth_headers)
    assert by_code.status_code == 200, by_code.text
    items = by_code.json()["items"]
    assert len(items) == 1
    assert items[0]["internal_patient_code"] == "P000001"
    # Never the stored real name: only the masked 姓某 form.
    assert items[0]["display_name"] == "张某"
    assert items[0]["encounter_count"] == 1

    by_name = client.get("/api/v1/patients?query=张三", headers=auth_headers)
    assert by_name.status_code == 200, by_name.text
    assert any(row["internal_patient_code"] == "P000001" for row in by_name.json()["items"])
    assert str(seeded.id) in {row["id"] for row in by_name.json()["items"]}

    miss = client.get("/api/v1/patients?query=李四", headers=auth_headers)
    assert miss.json()["items"] == []


def test_list_patients_pagination_and_identityless_summary(
    client: TestClient, auth_headers, db_session
) -> None:
    _seed_patient(db_session, "P000001", "张三", with_encounter=True)
    second = _seed_patient(db_session, "P000002")

    paged = client.get("/api/v1/patients?limit=1&offset=1", headers=auth_headers)
    assert paged.status_code == 200, paged.text
    body = paged.json()
    assert body["limit"] == 1
    assert body["offset"] == 1
    assert len(body["items"]) == 1
    # total counts the whole filtered set, so >50 patients stay browsable.
    assert body["total"] == 2

    searching = client.get("/api/v1/patients?query=P000002", headers=auth_headers)
    assert searching.json()["total"] == 1

    listing = client.get("/api/v1/patients", headers=auth_headers)
    body = listing.json()
    assert body["total"] == len(body["items"])
    rows = {row["internal_patient_code"]: row for row in body["items"]}
    # A patient without identity rows has no display name, never a masked guess.
    assert rows["P000002"]["id"] == str(second.id)
    assert rows["P000002"]["display_name"] is None
    assert rows["P000002"]["encounter_count"] == 0


def test_get_patient_404(client: TestClient, auth_headers) -> None:
    response = client.get(f"/api/v1/patients/{uuid.uuid4()}", headers=auth_headers)
    assert response.status_code == 404


def test_student_note_uncertain_save_replay(client, auth_headers, db_session):
    patient = _seed_patient(db_session, "P000030", with_encounter=True)
    encounter_id = patient.encounters[0].id
    url = f"/api/v1/encounters/{encounter_id}/student-notes"
    payload = {
        "note_kind": "student_observation",
        "content": "合成记录",
        "request_id": str(uuid.uuid4()),
    }
    first = client.post(url, headers=auth_headers, json=payload)
    assert first.status_code == 201, first.text
    # Simulate a successful commit whose response was lost: resend exactly.
    again = client.post(url, headers=auth_headers, json=payload)
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    detail = client.get(f"/api/v1/patients/{patient.id}", headers=auth_headers).json()
    assert len(detail["encounters"][0]["student_notes"]) == 1
    changed = client.post(url, headers=auth_headers, json={**payload, "content": "不同内容"})
    assert changed.status_code == 409
    other = _seed_patient(db_session, "P000031", with_encounter=True)
    cross = client.post(
        f"/api/v1/encounters/{other.encounters[0].id}/student-notes",
        headers=auth_headers,
        json=payload,
    )
    assert cross.status_code == 409


def test_committed_student_note_with_lost_response_converges_to_one_note(
    client: TestClient, auth_headers, db_session
) -> None:
    """“已提交但响应丢失”的补录恢复链：核对与补交都收敛到同一条记录。"""

    patient = _seed_patient(db_session, "P000034", with_encounter=True)
    encounter_id = patient.encounters[0].id
    url = f"/api/v1/encounters/{encounter_id}/student-notes"
    payload = {
        "note_kind": "student_observation",
        "content": "响应丢失的那次补录",
        "request_id": str(uuid.uuid4()),
    }
    committed = client.post(url, headers=auth_headers, json=payload)
    assert committed.status_code == 201, committed.text

    # 路径 A：核对优先——按请求标识查询命中，不补交，也只有一条记录。
    found = client.get(f"{url}/{payload['request_id']}", headers=auth_headers)
    assert found.status_code == 200, found.text
    detail = client.get(f"/api/v1/patients/{patient.id}", headers=auth_headers).json()
    notes = detail["encounters"][0]["student_notes"]
    assert len(notes) == 1

    # 路径 B：直接补交——同一请求标识原样重放，返回既有记录，总数不变。
    replay = client.post(url, headers=auth_headers, json=payload)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == payload["request_id"]
    detail = client.get(f"/api/v1/patients/{patient.id}", headers=auth_headers).json()
    notes = detail["encounters"][0]["student_notes"]
    assert len(notes) == 1
    assert notes[0]["id"] == payload["request_id"]


def test_patient_summary_view_and_single_encounter_detail(
    client: TestClient, auth_headers, db_session
) -> None:
    """The workspace date index uses the light summary view and loads each
    encounter's full content on demand; cross-patient lookups fail closed."""

    patient = _seed_patient(db_session, "P000020", with_encounter=True)
    digest = hashlib.sha256(b"extra-doc").hexdigest()
    document = Document(
        id=uuid.uuid4(),
        original_filename="second.jpg",
        original_file_path=f"synthetic/{digest}.jpg",
        sha256=digest,
        byte_size=10,
        mime_type="image/jpeg",
        document_type=DocumentTypeEnum.OUTPATIENT_RECORD,
        processing_status=ProcessingStatus.ACCEPTED,
    )
    encounter = Encounter(
        patient=patient,
        primary_document=document,
        primary_document_id=document.id,
        hospital="暨南大学附属第一医院",
        encounter_at=datetime(2026, 9, 1, 9, 30),
        chief_complaint_raw="咳嗽三天",
    )
    prescription = Prescription(
        encounter=encounter,
        evidence_document_id=document.id,
        total_decoctions=4,
        source_type=SourceTypeEnum.PHYSICIAN_RECORD,
    )
    prescription.items.append(
        PrescriptionItem(
            line_number=1,
            original_text="陈皮 6g",
            raw_drug_name="陈皮",
            canonical_drug_name="陈皮",
            normalization_status="normalized",
            parser_version="test-v1",
        )
    )
    prescription.items.append(
        PrescriptionItem(
            line_number=2,
            original_text="茯苓 10g",
            raw_drug_name="茯苓",
            normalization_status="pending",
            parser_version="test-v1",
        )
    )
    encounter.prescriptions.append(prescription)
    encounter.student_notes.append(
        StudentNote(
            patient=patient,
            encounter=encounter,
            note_kind=NoteKind.TEACHER_ORAL,
            content="老师指出宜理气化痰",
        )
    )
    db_session.add(document)
    db_session.add(encounter)
    db_session.commit()

    summary = client.get(f"/api/v1/patients/{patient.id}?view=summary", headers=auth_headers)
    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["view"] == "summary"
    assert body["encounter_count"] == 2
    assert len(body["encounters"]) == 2
    newest = body["encounters"][0]
    # 轻量摘要：只有计数字段，没有处方/症状正文。
    assert "prescriptions" not in newest
    assert "symptoms" not in newest
    assert newest["prescription_item_count"] == 2
    assert newest["student_note_count"] == 1
    assert newest["ai_analysis_count"] == 0
    assert newest["chief_complaint_raw"] == "咳嗽三天"

    detail = client.get(
        f"/api/v1/patients/{patient.id}/encounters/{encounter.id}",
        headers=auth_headers,
    )
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert detail_body["chief_complaint_raw"] == "咳嗽三天"
    assert len(detail_body["prescriptions"][0]["items"]) == 2
    assert detail_body["student_notes"][0]["content"] == "老师指出宜理气化痰"

    # 就诊不属于该患者时 404，绝不跨患者泄露。
    other = _seed_patient(db_session, "P000021", with_encounter=True)
    cross = client.get(
        f"/api/v1/patients/{other.id}/encounters/{encounter.id}",
        headers=auth_headers,
    )
    assert cross.status_code == 404

    missing = client.get(
        f"/api/v1/patients/{patient.id}/encounters/{uuid.uuid4()}",
        headers=auth_headers,
    )
    assert missing.status_code == 404


def test_create_student_note_persists_and_appears_in_timeline(
    client: TestClient, auth_headers, db_session
) -> None:
    patient_row = _seed_patient(db_session, "P000010", with_encounter=True)
    encounter_id = patient_row.encounters[0].id

    created = client.post(
        f"/api/v1/encounters/{encounter_id}/student-notes",
        headers=auth_headers,
        json={"note_kind": "teacher_oral", "content": "老师指出：脾虚湿困为本"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["note_kind"] == "teacher_oral"
    assert body["content"] == "老师指出：脾虚湿困为本"
    assert body["encounter_id"] == str(encounter_id)

    with SessionLocal() as session:
        note = session.get(StudentNote, uuid.UUID(body["id"]))
        assert note is not None
        assert note.patient_id == patient_row.id
        assert note.note_kind == NoteKind.TEACHER_ORAL

    detail = client.get(f"/api/v1/patients/{patient_row.id}", headers=auth_headers)
    assert detail.status_code == 200, detail.text
    notes = detail.json()["encounters"][0]["student_notes"]
    assert any(note["content"] == "老师指出：脾虚湿困为本" for note in notes)

    missing = client.post(
        f"/api/v1/encounters/{uuid.uuid4()}/student-notes",
        headers=auth_headers,
        json={"note_kind": "teacher_oral", "content": "无关就诊"},
    )
    assert missing.status_code == 404

    blank = client.post(
        f"/api/v1/encounters/{encounter_id}/student-notes",
        headers=auth_headers,
        json={"note_kind": "teacher_oral", "content": ""},
    )
    assert blank.status_code == 422


def test_get_student_note_by_id_supports_save_verification(
    client: TestClient, auth_headers, db_session
) -> None:
    """GET /encounters/{id}/student-notes/{note_id} answers “那次保存有没有入库？”

    A retryable save stores its request_id as the note row id; the timeline
    looks it up before replaying an uncertain save. Cross-encounter lookups
    fail closed with 404.
    """

    patient = _seed_patient(db_session, "P000032", with_encounter=True)
    encounter_id = patient.encounters[0].id
    url = f"/api/v1/encounters/{encounter_id}/student-notes"
    request_id = str(uuid.uuid4())
    created = client.post(
        url,
        headers=auth_headers,
        json={
            "note_kind": "student_observation",
            "content": "合成补录记录",
            "request_id": request_id,
        },
    )
    assert created.status_code == 201, created.text

    found = client.get(f"{url}/{request_id}", headers=auth_headers)
    assert found.status_code == 200, found.text
    assert found.json()["id"] == request_id
    assert found.json()["content"] == "合成补录记录"

    missing = client.get(f"{url}/{uuid.uuid4()}", headers=auth_headers)
    assert missing.status_code == 404

    other = _seed_patient(db_session, "P000033", with_encounter=True)
    cross = client.get(
        f"/api/v1/encounters/{other.encounters[0].id}/student-notes/{request_id}",
        headers=auth_headers,
    )
    assert cross.status_code == 404


def test_encounter_owner_resolves_positioning_only(
    client: TestClient, auth_headers, db_session
) -> None:
    """GET /encounters/{id}/owner answers “这条本机记录属于哪位患者？”.

    The workbench's local 补录 records carry only an encounter id; this
    authenticated lookup returns the owning patient id and NOTHING else —
    no clinical content — so the workbench can build an exact deep link.
    A missing encounter is an explicit 404, never a guessed owner.
    """

    patient = _seed_patient(db_session, "P000034", with_encounter=True)
    encounter_id = patient.encounters[0].id

    found = client.get(f"/api/v1/encounters/{encounter_id}/owner", headers=auth_headers)
    assert found.status_code == 200, found.text
    body = found.json()
    assert body == {"encounter_id": str(encounter_id), "patient_id": str(patient.id)}

    missing = client.get(f"/api/v1/encounters/{uuid.uuid4()}/owner", headers=auth_headers)
    assert missing.status_code == 404

    anonymous = client.get(f"/api/v1/encounters/{encounter_id}/owner")
    assert anonymous.status_code in {401, 403}
