"""Phase 9 apprenticeship writing tests (跟师笔记 / 医案整理)."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, func, select, update

from app.db.session import engine
from app.models.domain import (
    ApprenticeshipNoteVersion,
    AuditEvent,
    Diagnosis,
    Document,
    Encounter,
    Patient,
    PatientIdentity,
    Prescription,
    PrescriptionItem,
    StudentNote,
    SymptomObservation,
)
from app.models.enums import (
    DiagnosisType,
    DocumentType,
    DoseScope,
    DoseSource,
    NoteKind,
    ProcessingStatus,
    Sex,
    SourceType,
    SymptomStatus,
    SymptomTrend,
)
from app.services.ai.gateway import AIProviderResult
from app.services.ai.writing import (
    WritingContextBuilder,
    WritingRejected,
    render_case_record,
    render_teacher_note,
    validate_case_record_output,
    validate_teacher_note_output,
)

FIXTURE = Path(__file__).parent / "fixtures" / "patient_apprenticeship_notes.json"


def _preview_token(
    client,
    auth_headers,
    note_id: str,
    based_on_version_id: str,
    evidence_ids: list[str],
) -> dict:
    """取一次只读预览；断言成功并返回完整响应体（含 preview_token）。"""

    response = client.post(
        f"/api/v1/notes/{note_id}/evidence-preview",
        headers=auth_headers,
        json={
            "based_on_version_id": based_on_version_id,
            "evidence_encounter_ids": evidence_ids,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_manual_revision_retry_is_idempotent(client, auth_headers, db_session):
    patient, encounters = _seed_writing_patient(db_session)
    created = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "teacher_note",
            "content": "合成原稿",
            "encounter_ids": [str(encounters[0].id)],
        },
    )
    assert created.status_code == 201, created.text
    original = created.json()
    url = f"/api/v1/notes/{original['note_id']}/revisions"
    payload = {
        "content": "修改稿",
        "final": False,
        "based_on_version_id": original["id"],
        "request_id": str(uuid.uuid4()),
    }
    first = client.post(url, headers=auth_headers, json=payload)
    assert first.status_code == 201, first.text
    # A different later save must not turn a delayed retry into a new version.
    later = client.post(
        url, headers=auth_headers, json={**payload, "request_id": str(uuid.uuid4()), "final": True}
    )
    assert later.status_code == 201, later.text
    replay = client.post(url, headers=auth_headers, json=payload)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["version"] == first.json()["version"]
    history = client.get(f"/api/v1/notes/{original['note_id']}", headers=auth_headers).json()
    assert len(history["versions"]) == 3
    for change in (
        {"content": "其他内容"},
        {"final": True},
        {"based_on_version_id": later.json()["id"]},
    ):
        rejected = client.post(url, headers=auth_headers, json={**payload, **change})
        assert rejected.status_code == 409, rejected.text
    missing_base = client.post(
        url, headers=auth_headers, json={**payload, "based_on_version_id": None}
    )
    assert missing_base.status_code == 422


def test_committed_revision_with_lost_response_converges_to_one_version(
    client, auth_headers, db_session
) -> None:
    """“已提交但响应丢失”的完整恢复链：提交已入库、响应未到达客户端。

    客户端的两条恢复路径都必须收敛到同一个版本：先按请求标识查询（命中即
    不补交），或直接原样补交（服务端按请求标识幂等返回既有版本）。任何路
    径都不得产生第二个版本。
    """

    patient, encounters = _seed_writing_patient(db_session)
    created = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "teacher_note",
            "content": "合成原稿",
            "encounter_ids": [str(encounters[0].id)],
        },
    )
    assert created.status_code == 201, created.text
    request_id = str(uuid.uuid4())
    payload = {
        "content": "响应丢失的那次保存",
        "final": False,
        "based_on_version_id": created.json()["id"],
        "request_id": request_id,
    }
    url = f"/api/v1/notes/{created.json()['note_id']}/revisions"
    committed = client.post(url, headers=auth_headers, json=payload)
    assert committed.status_code == 201, committed.text

    # 路径 A：核对优先——按请求标识查询命中，客户端据此丢弃待确认记录，
    # 不再补交。此时历史里只有一个新版本。
    found = client.get(f"/api/v1/notes/versions/{request_id}", headers=auth_headers)
    assert found.status_code == 200, found.text
    assert found.json()["id"] == request_id
    history = client.get(f"/api/v1/notes/{created.json()['note_id']}", headers=auth_headers).json()
    assert len(history["versions"]) == 2

    # 路径 B：直接补交——同一请求标识原样重放，返回既有版本，历史不变。
    replay = client.post(url, headers=auth_headers, json=payload)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == request_id
    assert replay.json()["version"] == committed.json()["version"]
    history = client.get(f"/api/v1/notes/{created.json()['note_id']}", headers=auth_headers).json()
    assert len(history["versions"]) == 2
    assert sum(1 for item in history["versions"] if item["id"] == request_id) == 1


def test_revision_evidence_snapshots_inherit_and_adopt(
    client, auth_headers, db_session
) -> None:
    """修改文字逐字继承依据快照；采用新资料按当前库重算快照与哈希。

    依据快照固化该版本实际采用的写作材料（去标识上下文全文 + 各层版本号
    + 上下文哈希），使依据可重建而非只剩就诊 ID 与哈希。哈希必须始终等
    于快照内的哈希。
    """

    patient, encounters = _seed_writing_patient(db_session)
    created = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "teacher_note",
            "content": "合成原稿",
            "encounter_ids": [str(encounters[0].id)],
        },
    )
    assert created.status_code == 201, created.text
    original = created.json()
    url = f"/api/v1/notes/{original['note_id']}/revisions"
    snapshot = original["evidence_snapshot"]
    assert snapshot is not None
    assert snapshot["set_version"] == 1
    assert snapshot["context_hash"] == original["source_context_hash"]
    baseline_student_notes = snapshot["student_note_count"]
    assert baseline_student_notes >= 1

    # 修改文字（缺省证据字段）：快照逐字继承，哈希不变。
    inherit = client.post(
        url,
        headers=auth_headers,
        json={"content": "只改文字", "final": False, "based_on_version_id": original["id"]},
    )
    assert inherit.status_code == 201, inherit.text
    inherit_body = inherit.json()
    assert inherit_body["source_context_hash"] == original["source_context_hash"]
    assert inherit_body["evidence_snapshot"] == snapshot

    # 之后该就诊新增了一条学生补录。
    added = client.post(
        f"/api/v1/encounters/{encounters[0].id}/student-notes",
        headers=auth_headers,
        json={
            "note_kind": "teacher_oral",
            "content": "建快照之后才补录的老师口头信息",
            "request_id": str(uuid.uuid4()),
        },
    )
    assert added.status_code == 201, added.text

    # 采用新资料整理：同一就诊集合，按当前数据库重算快照与哈希。
    # 采用必须携带“基于所基于版本”的预览凭证（先看清楚再采用）。
    adopt_preview = _preview_token(
        client,
        auth_headers,
        original["note_id"],
        original["id"],
        [str(encounters[0].id)],
    )
    assert adopt_preview["based_on_version"]["id"] == original["id"]
    adopt = client.post(
        url,
        headers=auth_headers,
        json={
            "content": "按当前资料重新整理",
            "final": False,
            "based_on_version_id": original["id"],
            "evidence_encounter_ids": [str(encounters[0].id)],
            "preview_token": adopt_preview["proposal"]["preview_token"],
        },
    )
    assert adopt.status_code == 201, adopt.text
    adopt_body = adopt.json()
    assert adopt_body["evidence_encounters"] == [str(encounters[0].id)]
    assert adopt_body["source_context_hash"] == adopt_preview["proposal"]["source_context_hash"]
    assert adopt_body["source_context_hash"] != original["source_context_hash"]
    adopt_snapshot = adopt_body["evidence_snapshot"]
    assert adopt_snapshot is not None
    assert adopt_snapshot["student_note_count"] == baseline_student_notes + 1
    assert adopt_snapshot["context_hash"] == adopt_body["source_context_hash"]

    # 跟师笔记的新依据仍必须恰好一次就诊。
    two = client.post(
        url,
        headers=auth_headers,
        json={
            "content": "范围非法",
            "final": False,
            "based_on_version_id": original["id"],
            "evidence_encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        },
    )
    assert two.status_code == 422

    # 幂等重放把证据字段也纳入一致性：同标识不同依据 → 409。
    replay_token = _preview_token(
        client,
        auth_headers,
        original["note_id"],
        original["id"],
        [str(encounters[0].id)],
    )["proposal"]["preview_token"]
    replay_payload = {
        "content": "带依据的可重试保存",
        "final": False,
        "based_on_version_id": original["id"],
        "request_id": str(uuid.uuid4()),
        "evidence_encounter_ids": [str(encounters[0].id)],
        "preview_token": replay_token,
    }
    first = client.post(url, headers=auth_headers, json=replay_payload)
    assert first.status_code == 201, first.text
    replay = client.post(url, headers=auth_headers, json=replay_payload)
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]
    conflict = client.post(
        url,
        headers=auth_headers,
        json={**replay_payload, "evidence_encounter_ids": [str(encounters[1].id)]},
    )
    assert conflict.status_code == 409


def test_legacy_version_without_snapshot_is_labelled_and_never_backfilled(
    client, auth_headers, db_session
) -> None:
    """历史版本没有依据快照就如实标记为 NULL；继承不回填，显式采用才重建。"""

    patient, encounters = _seed_writing_patient(db_session)
    created = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "teacher_note",
            "content": "合成原稿",
            "encounter_ids": [str(encounters[0].id)],
        },
    )
    assert created.status_code == 201, created.text
    original = created.json()
    # 模拟本功能上线前的历史行：直接去掉快照。
    row = db_session.get(ApprenticeshipNoteVersion, uuid.UUID(original["id"]))
    assert row is not None
    row.evidence_snapshot = None
    db_session.commit()

    history = client.get(
        f"/api/v1/notes/{original['note_id']}", headers=auth_headers
    ).json()
    legacy = next(v for v in history["versions"] if v["id"] == original["id"])
    assert legacy["evidence_snapshot"] is None

    url = f"/api/v1/notes/{original['note_id']}/revisions"
    inherit = client.post(
        url,
        headers=auth_headers,
        json={"content": "继承自旧版本", "final": False, "based_on_version_id": original["id"]},
    )
    assert inherit.status_code == 201, inherit.text
    # 修改文字继承 NULL 快照：不按当前资料补成历史快照。
    assert inherit.json()["evidence_snapshot"] is None
    assert inherit.json()["source_context_hash"] == original["source_context_hash"]

    adopt_token = _preview_token(
        client,
        auth_headers,
        original["note_id"],
        original["id"],
        [str(encounters[0].id)],
    )["proposal"]["preview_token"]
    adopt = client.post(
        url,
        headers=auth_headers,
        json={
            "content": "显式采用当前资料",
            "final": False,
            "based_on_version_id": original["id"],
            "evidence_encounter_ids": [str(encounters[0].id)],
            "preview_token": adopt_token,
        },
    )
    assert adopt.status_code == 201, adopt.text
    # 采用新资料是显式的“现在”操作，重建的快照标注的就是当前材料。
    assert adopt.json()["evidence_snapshot"] is not None
    adopt_snapshot = adopt.json()["evidence_snapshot"]
    assert adopt_snapshot["context_hash"] == adopt.json()["source_context_hash"]
    # 数据未变时重建哈希与旧哈希相同——上下文是确定性的，这正是可重建性的证明。
    assert adopt.json()["source_context_hash"] == original["source_context_hash"]


def test_global_note_library_pagination_and_filters(
    client, auth_headers, db_session
) -> None:
    """写作库服务端分页与筛选：total 全集计数，页内只含摘要。"""

    patient, encounters = _seed_writing_patient(db_session)
    note_ids: list[str] = []
    for index in range(1, 4):
        created = client.post(
            f"/api/v1/patients/{patient.id}/notes",
            headers=auth_headers,
            json={
                "note_type": "case_record",
                "content": f"医案第 {index} 篇。",
                "encounter_ids": [str(encounters[0].id)],
            },
        )
        assert created.status_code == 201, created.text
        note_ids.append(created.json()["note_id"])
    # 第三篇定稿，用于 finalized 筛选。
    finalized = client.post(
        f"/api/v1/notes/{note_ids[2]}/revisions",
        headers=auth_headers,
        json={"content": "第三篇定稿。", "final": True},
    )
    assert finalized.status_code == 201, finalized.text

    # 分页：limit=2, offset=0 → 两篇 + 全集 total=3。
    page1 = client.get(
        "/api/v1/notes?limit=2&offset=0", headers=auth_headers
    ).json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert page1["limit"] == 2 and page1["offset"] == 0
    page2 = client.get(
        "/api/v1/notes?limit=2&offset=2", headers=auth_headers
    ).json()
    assert len(page2["items"]) == 1
    assert {item["note_id"] for item in page1["items"]}.isdisjoint(
        {item["note_id"] for item in page2["items"]}
    )
    # 页内是摘要：没有正文。
    assert all("content" not in item["latest"] for item in [*page1["items"], *page2["items"]])

    # 筛选：只看已定稿 → 第三篇。
    finalized_page = client.get(
        "/api/v1/notes?finalized=true", headers=auth_headers
    ).json()
    assert finalized_page["total"] == 1
    assert finalized_page["items"][0]["note_id"] == note_ids[2]
    # 筛选：按患者 → 该患者的 3 篇。
    by_patient = client.get(
        f"/api/v1/notes?patient_id={patient.id}", headers=auth_headers
    ).json()
    assert by_patient["total"] == 3
    # 筛选：按类型 → 医案 3 篇；跟师笔记 0 篇。
    by_type = client.get("/api/v1/notes?note_type=case_record", headers=auth_headers).json()
    assert by_type["total"] == 3
    teacher_only = client.get(
        "/api/v1/notes?note_type=teacher_note", headers=auth_headers
    ).json()
    assert teacher_only["total"] == 0
    # 非法 limit/offset 收敛到安全范围，而不是报错。
    clamped = client.get("/api/v1/notes?limit=99999&offset=-5", headers=auth_headers).json()
    assert clamped["offset"] == 0
    assert len(clamped["items"]) == clamped["total"]


def _document(label: str) -> Document:
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


def _seed_writing_patient(db_session) -> tuple[Patient, list[Encounter]]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    patient = Patient(internal_patient_code=fixture["patient_code"], sex=Sex.FEMALE)
    patient.identities.append(
        PatientIdentity(
            hospital="合成测试医院",
            real_name="测试姓名",
            hospital_medical_record_number="SYN-PRIVATE-001",
        )
    )
    encounters: list[Encounter] = []
    for encounter_payload in fixture["encounters"]:
        source = SourceType(encounter_payload["source"])
        document = _document(encounter_payload["label"])
        encounter = Encounter(
            patient=patient,
            primary_document=document,
            primary_document_id=document.id,
            hospital="合成测试医院",
            encounter_at=datetime.fromisoformat(encounter_payload["encounter_at"]),
            chief_complaint_raw=encounter_payload["chief_complaint"],
            raw_source_type=source,
        )
        for symptom_payload in encounter_payload["symptoms"]:
            encounter.symptoms.append(
                SymptomObservation(
                    symptom_raw=symptom_payload["name"],
                    symptom_normalized=symptom_payload["normalized"],
                    status=SymptomStatus.PRESENT,
                    trend=SymptomTrend(symptom_payload["trend"]),
                    source_type=source,
                    evidence_document_id=document.id,
                )
            )
        encounter.diagnoses.append(
            Diagnosis(
                diagnosis_type=DiagnosisType.TCM_PATTERN,
                name_raw=encounter_payload["diagnosis"],
                name_normalized=encounter_payload["diagnosis"],
                is_primary=True,
                source_type=source,
                evidence_document_id=document.id,
            )
        )
        prescription = Prescription(
            encounter=encounter,
            evidence_document_id=document.id,
            total_decoctions=7,
            source_type=source,
        )
        for line_number, item_payload in enumerate(encounter_payload["items"], start=1):
            dose = item_payload["dose"]
            prescription.items.append(
                PrescriptionItem(
                    line_number=line_number,
                    original_text=f"{item_payload['drug']} synthetic evidence",
                    raw_drug_name=item_payload["drug"],
                    canonical_drug_name=item_payload["drug"],
                    calculated_dose_g=Decimal(dose) if dose else None,
                    dose_source=DoseSource.CALCULATED if dose else DoseSource.UNKNOWN,
                    dose_scope=DoseScope(item_payload["scope"]),
                    normalization_status="normalized",
                    parser_version="synthetic-writing-v1",
                )
            )
        if encounter_payload.get("student_note"):
            encounter.student_notes.append(
                StudentNote(
                    patient=patient,
                    note_kind=NoteKind(encounter_payload["student_note"]["note_kind"]),
                    content=encounter_payload["student_note"]["content"],
                )
            )
        encounters.append(encounter)
    db_session.add(patient)
    db_session.commit()
    return patient, encounters


def _make_ref(context: dict[str, Any], ref_id: str, keys: list[str], claim: str) -> dict[str, Any]:
    entries = [context["evidence_catalog"][key] for key in keys]
    encounter_ids = sorted(
        {encounter_id for entry in entries for encounter_id in entry["encounter_ids"]}
    )
    sources = {entry["evidence_type"] for entry in entries}
    evidence_type = next(iter(sources)) if len(sources) == 1 else "mixed"
    return {
        "evidence_ref_id": ref_id,
        "claim": claim,
        "evidence_encounter_ids": encounter_ids,
        "evidence_type": evidence_type,
        "evidence_keys": keys,
    }


def _diff_item_key(diff: dict[str, Any], *, change: str) -> str:
    return next(item["evidence_key"] for item in diff["items"] if change in item["change_types"])


def _teacher_output(context: dict[str, Any]) -> dict[str, Any]:
    encounter = context["encounter"]
    encounter_id = encounter["encounter_id"]
    refs: list[dict[str, Any]] = []

    def ref(ref_id: str, keys: list[str], claim: str) -> str:
        refs.append(_make_ref(context, ref_id, keys, claim))
        return ref_id

    chief = ref("chief", [encounter["recorded_fields"][0]["evidence_key"]], "主诉记录")
    symptom = ref("symptom", [encounter["symptoms"][0]["evidence_key"]], "症状记录")
    diagnosis = ref(
        "diagnosis",
        [f"encounters.{encounter_id}.diagnoses.index"],
        "诊断记录索引",
    )
    prescription_item = encounter["prescriptions"][0]["items"][0]["evidence_key"]
    rx = ref("rx", [prescription_item], "处方记录")

    diff = encounter["prescription_diff_from_previous"]
    changes: list[dict[str, Any]] = []
    interpretations: list[dict[str, Any]] = []
    if diff is not None:
        dose_up = ref("dose_up", [_diff_item_key(diff, change="DOSE_INCREASED")], "剂量增加")
        added = ref("added", [_diff_item_key(diff, change="ADDED")], "新增药物")
        changes = [
            {
                "text": "白术剂量较前增加（确定性结论）。",
                "evidence_ref_ids": [dose_up],
            },
            {
                "text": "陈皮为本次新增药物（确定性结论）。",
                "evidence_ref_ids": [added],
            },
        ]
        interpretations = [
            {
                "text": "从记录看，本次加量可能与症状持续有关，尚不能确定。",
                "evidence_ref_ids": [dose_up],
                "provenance": "ai_inferred",
                "tentative": True,
            }
        ]
    else:
        interpretations = [
            {
                "text": "从记录看，本次诊疗思路的记录有限，尚不能确定，供参考。",
                "evidence_ref_ids": [symptom],
                "provenance": "ai_inferred",
                "tentative": True,
            }
        ]

    return {
        "visit_overview": {"text": "本次复诊主诉见记录。", "evidence_ref_ids": [chief]},
        "four_examinations": [{"text": "症状记录以输入为准。", "evidence_ref_ids": [symptom]}],
        "diagnosis_and_prescription": [
            {"text": "诊断与处方以记录为准。", "evidence_ref_ids": [diagnosis, rx]}
        ],
        "changes_from_previous": changes,
        "teacher_reasoning_interpretation": interpretations,
        "student_reflection": [
            {
                "text": "可以理解为一次谨慎的剂量调整，供参考。",
                "evidence_ref_ids": [rx],
                "provenance": "ai_inferred",
                "tentative": True,
            }
        ],
        "questions_for_teacher": [
            {
                "question": "下次复诊时剂量调整的依据是什么？",
                "evidence_ref_ids": [rx],
            }
        ],
        "evidence_refs": refs,
    }


def _case_output(context: dict[str, Any]) -> dict[str, Any]:
    refs: list[dict[str, Any]] = []
    encounters = context["encounters"]

    def ref(ref_id: str, keys: list[str], claim: str) -> str:
        refs.append(_make_ref(context, ref_id, keys, claim))
        return ref_id

    first = encounters[0]
    basic = ref("basic", ["patient.overview"], "去标识化基本情况")
    chief = ref("chief", [first["recorded_fields"][0]["evidence_key"]], "初诊主诉")
    symptom_keys = [encounter["symptoms"][0]["evidence_key"] for encounter in encounters]
    symptoms = ref("symptoms", symptom_keys, "主要症候记录")
    visit_sections = []
    for encounter in encounters:
        visit_chief = ref(
            f"visit_{encounter['encounter_id'][:8]}",
            [encounter["recorded_fields"][0]["evidence_key"]],
            "本次就诊主诉",
        )
        visit_sections.append(
            {
                "encounter_id": encounter["encounter_id"],
                "date": encounter["date"],
                "statements": [{"text": "就诊经过以记录为准。", "evidence_ref_ids": [visit_chief]}],
            }
        )

    prescription_changes = []
    if len(encounters) > 1:
        pair = (encounters[0]["encounter_id"], encounters[1]["encounter_id"])
        summary_key = f"diffs.{pair[0]}.{pair[1]}.summary"
        diff_ref = ref("diff", [summary_key], "确定性处方Diff摘要")
        change_types = sorted(
            {
                change
                for item in encounters[1]["prescription_diff_from_previous"]["items"]
                for change in item["change_types"]
            }
        )
        prescription_changes.append(
            {
                "previous_encounter_id": pair[0],
                "current_encounter_id": pair[1],
                "summary": "处方变化以确定性 Diff 为准。",
                "deterministic_changes": change_types,
                "evidence_ref_ids": [diff_ref],
            }
        )

    return {
        "basic_information": {"text": "基本情况按去标识化记录整理。", "evidence_ref_ids": [basic]},
        "chief_complaint": {"text": "主诉以初诊记录为准。", "evidence_ref_ids": [chief]},
        "main_symptoms": [{"text": "主要症候以记录为准。", "evidence_ref_ids": [symptoms]}],
        "visit_sections": visit_sections,
        "prescription_changes": prescription_changes,
        "course_evolution": {
            "text": "病程演变以记录为准。",
            "evidence_ref_ids": [symptoms],
        },
        "commentary": [
            {
                "text": "从记录看，复诊以调整运脾力度为主，供参考。",
                "evidence_ref_ids": prescription_changes[0]["evidence_ref_ids"]
                if prescription_changes
                else [chief],
                "provenance": "ai_inferred",
                "tentative": True,
            }
        ],
        "evidence_refs": refs,
    }


class StubWritingProvider:
    """Deterministic provider that emits schema-valid drafts from the context."""

    def __init__(self, invalid: str | None = None) -> None:
        self.invalid = invalid
        self.contexts: list[dict[str, Any]] = []

    def run(
        self, *, prompt: str, context: dict[str, Any], schema: dict[str, Any]
    ) -> AIProviderResult:
        self.contexts.append(context)
        if "encounters" in context:
            output = _case_output(context)
            if self.invalid == "diff":
                output["prescription_changes"][0]["deterministic_changes"] = ["DOSE_DECREASED"]
            elif self.invalid == "visits":
                output["visit_sections"] = output["visit_sections"][:1]
        else:
            output = _teacher_output(context)
            if self.invalid == "attribution":
                output["teacher_reasoning_interpretation"] = [
                    {
                        "text": "老师指出白术需加量（学习理解）。",
                        "evidence_ref_ids": ["symptom"],
                        "provenance": "ai_inferred",
                        "tentative": True,
                    }
                ]
        return AIProviderResult(
            provider="stub",
            model="writing-test",
            output=json.dumps(output, ensure_ascii=False),
            structured_output=output,
        )


# ---------------------------------------------------------------------------
# Context builder + validation
# ---------------------------------------------------------------------------


def test_teacher_note_context_is_deidentified_and_registers_phase7_diff(db_session) -> None:
    patient, encounters = _seed_writing_patient(db_session)
    context = WritingContextBuilder().build_teacher_note_context(
        patient, encounters[1], encounters[0], ["测试姓名", "SYN-PRIVATE-001"]
    )
    payload = context.model_dump(mode="json")

    assert payload["patient"]["patient_code"] == "P-SYN-0901"
    assert payload["encounter"]["prescription_diff_from_previous"] is not None
    assert payload["encounter"]["has_previous_encounter"] is True
    diff = payload["encounter"]["prescription_diff_from_previous"]
    assert any(item["dose_change"] == "INCREASED" for item in diff["items"])
    assert diff["evidence_key"] in payload["evidence_catalog"]
    # The single-encounter context carries only that encounter's student notes.
    second_note_kinds = {
        entry.metadata["note_kind"]
        for entry in context.evidence_catalog.values()
        if "note_kind" in entry.metadata
    }
    assert second_note_kinds == {"student_observation"}
    first_context = WritingContextBuilder().build_teacher_note_context(
        patient, encounters[0], None, []
    )
    first_note_kinds = {
        entry.metadata["note_kind"]
        for entry in first_context.evidence_catalog.values()
        if "note_kind" in entry.metadata
    }
    assert "teacher_oral" in first_note_kinds
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "测试姓名" not in serialized
    assert "SYN-PRIVATE-001" not in serialized


def test_teacher_note_validation_guardrails(db_session) -> None:
    patient, encounters = _seed_writing_patient(db_session)
    builder = WritingContextBuilder()

    first_context = builder.build_teacher_note_context(patient, encounters[0], None, [])
    valid_first = _teacher_output(first_context.model_dump(mode="json"))
    assert validate_teacher_note_output(valid_first, first_context) is not None
    # First visit: no diff to compare against.
    valid_first["changes_from_previous"] = [{"text": "白术加量。", "evidence_ref_ids": ["rx"]}]
    with pytest.raises(WritingRejected):
        validate_teacher_note_output(valid_first, first_context)

    second_context = builder.build_teacher_note_context(patient, encounters[1], encounters[0], [])
    second_payload = second_context.model_dump(mode="json")
    valid = _teacher_output(second_payload)
    assert validate_teacher_note_output(valid, second_context) is not None

    # Unknown evidence key.
    tampered = _teacher_output(second_payload)
    tampered["evidence_refs"][0]["evidence_keys"] = ["invented.fact"]
    with pytest.raises(WritingRejected):
        validate_teacher_note_output(tampered, second_context)

    # Section 四 must cite the deterministic diff.
    no_diff = _teacher_output(second_payload)
    no_diff["changes_from_previous"][0]["evidence_ref_ids"] = ["symptom"]
    with pytest.raises(WritingRejected):
        validate_teacher_note_output(no_diff, second_context)

    # Teacher attribution without a recorded teacher_oral citation.
    attribution = _teacher_output(second_payload)
    attribution["teacher_reasoning_interpretation"] = [
        {
            "text": "老师指出白术需加量（学习理解）。",
            "evidence_ref_ids": ["symptom"],
            "provenance": "ai_inferred",
            "tentative": True,
        }
    ]
    with pytest.raises(WritingRejected):
        validate_teacher_note_output(attribution, second_context)

    # Attribution is allowed when the cited note is a recorded teacher_oral
    # note of the same encounter (available on the first visit's context).
    oral_key = next(
        key
        for key, entry in first_context.evidence_catalog.items()
        if entry.metadata.get("note_kind") == "teacher_oral"
    )
    first_payload = first_context.model_dump(mode="json")
    oral_ref = _make_ref(first_payload, "oral", [oral_key], "老师口头记录")
    allowed = _teacher_output(first_payload)
    allowed["evidence_refs"].append(oral_ref)
    allowed["teacher_reasoning_interpretation"] = [
        {
            "text": "老师指出脾虚宜运脾，不必峻补（学习理解）。",
            "evidence_ref_ids": ["oral"],
            "provenance": "ai_inferred",
            "tentative": True,
        }
    ]
    assert validate_teacher_note_output(allowed, first_context) is not None

    # Non-tentative interpretation wording is rejected by the schema itself.
    blunt = _teacher_output(second_payload)
    blunt["student_reflection"] = [
        {
            "text": "这次加量就是为了健脾。",
            "evidence_ref_ids": ["rx"],
            "provenance": "ai_inferred",
            "tentative": True,
        }
    ]
    with pytest.raises(WritingRejected):
        validate_teacher_note_output(blunt, second_context)

    rendered = render_teacher_note(
        validate_teacher_note_output(valid, second_context), second_context
    )
    assert "一、就诊概况" in rendered
    assert "四、与前诊相比的变化" in rendered
    assert "七、值得进一步请教的问题" in rendered
    assert str(encounters[1].id) in rendered


def test_case_record_validation_guardrails(db_session) -> None:
    patient, encounters = _seed_writing_patient(db_session)
    builder = WritingContextBuilder()

    single_context = builder.build_case_record_context(patient, [encounters[0]], [])
    single_payload = single_context.model_dump(mode="json")
    single = _case_output(single_payload)
    assert single["prescription_changes"] == []
    validated = validate_case_record_output(single, single_context)
    rendered = render_case_record(validated, single_context)
    assert "【初诊】" in rendered
    assert "【按语】" in rendered
    assert str(encounters[0].id) in rendered

    range_context = builder.build_case_record_context(patient, [encounters[0], encounters[1]], [])
    range_payload = range_context.model_dump(mode="json")
    valid = _case_output(range_payload)
    assert validate_case_record_output(valid, range_context) is not None

    # Visit sections must cover every encounter in order.
    missing_visit = _case_output(range_payload)
    missing_visit["visit_sections"] = missing_visit["visit_sections"][:1]
    with pytest.raises(WritingRejected):
        validate_case_record_output(missing_visit, range_context)

    # Deterministic change types must be echoed, never recalculated.
    altered = _case_output(range_payload)
    altered["prescription_changes"][0]["deterministic_changes"] = ["DOSE_DECREASED"]
    with pytest.raises(WritingRejected):
        validate_case_record_output(altered, range_context)

    # Adjacent-pair coverage is mandatory.
    uncovered = _case_output(range_payload)
    uncovered["prescription_changes"] = []
    with pytest.raises(WritingRejected):
        validate_case_record_output(uncovered, range_context)


# ---------------------------------------------------------------------------
# API behaviour
# ---------------------------------------------------------------------------


def test_teacher_note_api_generates_and_appends_versions(
    client, auth_headers, db_session, monkeypatch
) -> None:
    patient, encounters = _seed_writing_patient(db_session)
    stub = StubWritingProvider()
    monkeypatch.setattr(
        "app.api.routes.notes.get_apprenticeship_writing_provider",
        lambda session: stub,
    )

    created = client.post(
        f"/api/v1/encounters/{encounters[1].id}/teacher-note-drafts",
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["version"] == 1
    assert body["version_type"] == "ai_draft"
    assert body["source"] == "ai_inferred"
    assert body["evidence_encounters"] == [str(encounters[1].id)]
    assert "一、就诊概况" in body["content"]
    assert "内容依据" in body["content"]
    assert body["structured_output"]["visit_overview"]["text"]
    assert len(body["source_context_hash"]) == 64
    assert "测试姓名" not in json.dumps(stub.contexts, ensure_ascii=False)

    note_id = body["note_id"]
    first_content = body["content"]

    regenerated = client.post(f"/api/v1/notes/{note_id}/regenerate", headers=auth_headers)
    assert regenerated.status_code == 201, regenerated.text
    assert regenerated.json()["version"] == 2
    assert regenerated.json()["version_type"] == "ai_draft"

    draft_saved = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={"content": "我改过的草稿。", "final": False},
    )
    assert draft_saved.status_code == 201, draft_saved.text
    assert draft_saved.json()["version_type"] == "user_draft"

    final_saved = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={"content": "我的定稿。", "final": True},
    )
    assert final_saved.status_code == 201, final_saved.text
    assert final_saved.json()["version_type"] == "user_final"

    history = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers)
    assert history.status_code == 200
    versions = history.json()["versions"]
    assert [item["version"] for item in versions] == [1, 2, 3, 4]
    assert versions[0]["content"] == first_content  # never overwritten
    assert versions[1]["based_on_version_id"] == versions[0]["id"]
    assert versions[2]["version_type"] == "user_draft"
    assert versions[3]["source"] == "student_note"
    assert versions[3]["structured_output"] is None
    assert versions[3]["provider"] is None

    listed = client.get(f"/api/v1/patients/{patient.id}/notes", headers=auth_headers)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert items[0]["note_id"] == note_id
    assert items[0]["version_count"] == 4
    assert items[0]["latest"]["version_type"] == "user_final"


def test_case_record_api_range_and_fail_closed(
    client, auth_headers, db_session, monkeypatch
) -> None:
    patient, encounters = _seed_writing_patient(db_session)

    monkeypatch.setattr(
        "app.api.routes.notes.get_apprenticeship_writing_provider",
        lambda session: None,
    )
    missing_provider = client.post(
        f"/api/v1/patients/{patient.id}/case-record-drafts",
        headers=auth_headers,
        json={},
    )
    assert missing_provider.status_code == 503
    assert db_session.scalar(select(func.count(ApprenticeshipNoteVersion.id))) == 0

    failing = StubWritingProvider(invalid="diff")
    monkeypatch.setattr(
        "app.api.routes.notes.get_apprenticeship_writing_provider",
        lambda session: failing,
    )
    rejected = client.post(
        f"/api/v1/patients/{patient.id}/case-record-drafts",
        headers=auth_headers,
        json={},
    )
    assert rejected.status_code == 502
    assert db_session.scalar(select(func.count(ApprenticeshipNoteVersion.id))) == 0

    stub = StubWritingProvider()
    monkeypatch.setattr(
        "app.api.routes.notes.get_apprenticeship_writing_provider",
        lambda session: stub,
    )
    created = client.post(
        f"/api/v1/patients/{patient.id}/case-record-drafts",
        headers=auth_headers,
        json={
            "start_encounter_id": str(encounters[0].id),
            "end_encounter_id": str(encounters[1].id),
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["note_type"] == "case_record"
    assert "【初诊】" in body["content"]
    assert "【方药变化】" in body["content"]
    assert str(encounters[0].id) in body["content"]
    assert str(encounters[1].id) in body["content"]
    changes = body["structured_output"]["prescription_changes"]
    assert changes[0]["previous_encounter_id"] == str(encounters[0].id)
    assert "DOSE_INCREASED" in changes[0]["deterministic_changes"]

    single = client.post(
        f"/api/v1/patients/{patient.id}/case-record-drafts",
        headers=auth_headers,
        json={
            "start_encounter_id": str(encounters[0].id),
            "end_encounter_id": str(encounters[0].id),
        },
    )
    assert single.status_code == 201, single.text
    assert single.json()["structured_output"]["prescription_changes"] == []

    inverted = client.post(
        f"/api/v1/patients/{patient.id}/case-record-drafts",
        headers=auth_headers,
        json={
            "start_encounter_id": str(encounters[1].id),
            "end_encounter_id": str(encounters[0].id),
        },
    )
    assert inverted.status_code == 422


def test_user_original_note_keeps_user_text_through_ai_and_final(
    client, auth_headers, db_session, monkeypatch
) -> None:
    patient, encounters = _seed_writing_patient(db_session)
    stub = StubWritingProvider()
    monkeypatch.setattr(
        "app.api.routes.notes.get_apprenticeship_writing_provider",
        lambda session: stub,
    )

    original = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "teacher_note",
            "content": "我自己的跟师记录原文。",
            "encounter_ids": [str(encounters[0].id)],
        },
    )
    assert original.status_code == 201, original.text
    assert original.json()["version_type"] == "user_original"
    note_id = original.json()["note_id"]

    # AI 整理 appends a draft; it must not rewrite the user's original text.
    ai_draft = client.post(f"/api/v1/notes/{note_id}/regenerate", headers=auth_headers)
    assert ai_draft.status_code == 201, ai_draft.text
    assert ai_draft.json()["version_type"] == "ai_draft"

    finalized = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={"content": "在 AI 草稿基础上定稿。", "final": True},
    )
    assert finalized.status_code == 201, finalized.text

    history = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers)
    versions = history.json()["versions"]
    assert [item["version_type"] for item in versions] == [
        "user_original",
        "ai_draft",
        "user_final",
    ]
    # 用户原稿正文保持原样;展示层在末尾追加系统维护的内容依据块。
    assert versions[0]["content"].startswith("我自己的跟师记录原文。")
    assert "内容依据（Encounter IDs）：" in versions[0]["content"]
    assert str(encounters[0].id) in versions[0]["content"]
    stored_original = db_session.scalar(
        select(ApprenticeshipNoteVersion).where(
            ApprenticeshipNoteVersion.note_id == uuid.UUID(note_id),
            ApprenticeshipNoteVersion.version == 1,
        )
    )
    assert stored_original is not None
    # 存储行保存的仍是用户逐字原文,未被改写。
    assert stored_original.content == "我自己的跟师记录原文。"

    # 跟师笔记 user_original must be tied to exactly one encounter.
    invalid_scope = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "teacher_note",
            "content": "两诊不限",
            "encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        },
    )
    assert invalid_scope.status_code == 422

    foreign_base = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={
            "content": "x",
            "based_on_version_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    assert foreign_base.status_code == 422


def test_evidence_appendix_is_system_maintained_across_revisions(
    client, auth_headers, db_session, monkeypatch
) -> None:
    """Deleting 内容依据 in the editor cannot strip evidence from the note.

    The stored row keeps the authored bytes; the read model deterministically
    re-attaches the appendix from ``evidence_encounter_ids`` and never
    duplicates a block that is already present.
    """

    patient, encounters = _seed_writing_patient(db_session)
    monkeypatch.setattr(
        "app.api.routes.notes.get_apprenticeship_writing_provider",
        lambda session: None,
    )

    original = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "case_record",
            "content": "我的原始跟师记录。",
            "encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        },
    )
    assert original.status_code == 201, original.text
    note_id = original.json()["note_id"]
    evidence_ids = original.json()["evidence_encounters"]

    presented = original.json()["content"]
    assert presented.startswith("我的原始跟师记录。")
    assert presented.count("内容依据（Encounter IDs）：") == 1
    for value in evidence_ids:
        assert value in presented
    stored = db_session.scalar(
        select(ApprenticeshipNoteVersion).where(
            ApprenticeshipNoteVersion.note_id == uuid.UUID(note_id)
        )
    )
    assert stored is not None
    assert stored.content == "我的原始跟师记录。"

    # A revision that deletes the appendix keeps its evidence on read-back.
    stripped = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={"content": "我删掉了内容依据。", "final": False},
    )
    assert stripped.status_code == 201, stripped.text
    revised = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers)
    latest = revised.json()["versions"][-1]
    assert latest["content"].startswith("我删掉了内容依据。")
    assert latest["content"].count("内容依据（Encounter IDs）：") == 1
    for value in evidence_ids:
        assert value in latest["content"]

    # Saving the presented text back is idempotent: no duplicated block.
    resaved = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={"content": latest["content"], "final": True},
    )
    assert resaved.status_code == 201, resaved.text
    assert resaved.json()["content"] == latest["content"]
    history = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers)
    final_version = history.json()["versions"][-1]
    assert final_version["content"].count("内容依据（Encounter IDs）：") == 1
    stored_final = db_session.scalar(
        select(ApprenticeshipNoteVersion)
        .where(ApprenticeshipNoteVersion.note_id == uuid.UUID(note_id))
        .order_by(ApprenticeshipNoteVersion.version.desc())
        .limit(1)
    )
    assert stored_final is not None
    assert stored_final.version_type == "user_final"


def test_note_revision_version_conflict_retries_and_appends(
    client, auth_headers, db_session, monkeypatch
) -> None:
    """Two windows racing on "latest + 1" must not 500 or overwrite.

    The first version read is stale (another window committed that number
    before our flush); the unique constraint fires and the route retries
    with a freshly read maximum, keeping the chain append-only.
    """

    patient, encounters = _seed_writing_patient(db_session)
    original = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "case_record",
            "content": "第一版。",
            "encounter_ids": [str(encounters[0].id)],
        },
    )
    assert original.status_code == 201, original.text
    note_id = original.json()["note_id"]
    second = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={"content": "第二版。", "final": False},
    )
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2

    from app.api.routes import notes as notes_module

    real_next = notes_module._next_note_version
    calls = {"count": 0}

    def stale_next(session, stale_note_id):
        calls["count"] += 1
        if calls["count"] == 1:
            return 2  # another window already claimed version 2
        return real_next(session, stale_note_id)

    monkeypatch.setattr(notes_module, "_next_note_version", stale_next)

    third = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={"content": "第三版。", "final": True},
    )
    assert third.status_code == 201, third.text
    assert third.json()["version"] == 3

    history = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers)
    assert [item["version"] for item in history.json()["versions"]] == [1, 2, 3]
    assert [item["content"] for item in history.json()["versions"]][1].startswith("第二版。")
    assert calls["count"] >= 2


def test_regenerate_version_conflict_retries_without_second_ai_call(
    client, auth_headers, db_session, monkeypatch
) -> None:
    """A regenerate racing another window retries the append, not the AI call."""

    patient, encounters = _seed_writing_patient(db_session)
    stub = StubWritingProvider()
    monkeypatch.setattr(
        "app.api.routes.notes.get_apprenticeship_writing_provider",
        lambda session: stub,
    )
    created = client.post(
        f"/api/v1/encounters/{encounters[0].id}/teacher-note-drafts",
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["note_id"]
    revision = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={"content": "用户定稿。", "final": True},
    )
    assert revision.status_code == 201, revision.text

    from app.api.routes import notes as notes_module

    real_next = notes_module._next_note_version
    calls = {"count": 0}

    def stale_next(session, stale_note_id):
        calls["count"] += 1
        if calls["count"] == 1:
            return 2  # another window already claimed version 2
        return real_next(session, stale_note_id)

    monkeypatch.setattr(notes_module, "_next_note_version", stale_next)

    ai_calls_before = len(stub.contexts)
    regenerated = client.post(f"/api/v1/notes/{note_id}/regenerate", headers=auth_headers)
    assert regenerated.status_code == 201, regenerated.text
    assert regenerated.json()["version"] == 3
    assert regenerated.json()["version_type"] == "ai_draft"
    # The provider ran exactly once for this regenerate; the version-conflict
    # retry rebuilt only the row, never the AI call.
    assert len(stub.contexts) == ai_calls_before + 1

    history = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers)
    assert [item["version_type"] for item in history.json()["versions"]] == [
        "ai_draft",
        "user_final",
        "ai_draft",
    ]


def test_patient_note_list_batches_evidence_lookups(client, auth_headers, db_session) -> None:
    """Listing notes loads evidence dates once — not once per version."""

    from sqlalchemy import event

    from app.db.session import engine

    patient, encounters = _seed_writing_patient(db_session)
    created = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "case_record",
            "content": "原始记录。",
            "encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        },
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["note_id"]
    for index in range(3):
        revision = client.post(
            f"/api/v1/notes/{note_id}/revisions",
            headers=auth_headers,
            json={"content": f"第{index + 2}版。", "final": False},
        )
        assert revision.status_code == 201, revision.text

    encounter_queries = {"count": 0}

    def count_encounter_queries(conn, cursor, statement, parameters, context, executemany):
        if "FROM encounters" in statement:
            encounter_queries["count"] += 1

    event.listen(engine, "before_cursor_execute", count_encounter_queries)
    try:
        response = client.get(f"/api/v1/patients/{patient.id}/notes", headers=auth_headers)
    finally:
        event.remove(engine, "before_cursor_execute", count_encounter_queries)

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["version_count"] == 4
    # Two batched lookups regardless of version count: one for evidence dates,
    # one for the patient's encounter rows (适用范围 scope hints). The
    # pre-batching implementation issued one per version (4).
    assert encounter_queries["count"] == 2


def test_ai_draft_rejection_keeps_prior_versions_intact(
    client, auth_headers, db_session, monkeypatch
) -> None:
    patient, encounters = _seed_writing_patient(db_session)
    stub = StubWritingProvider()
    monkeypatch.setattr(
        "app.api.routes.notes.get_apprenticeship_writing_provider",
        lambda session: stub,
    )
    created = client.post(
        f"/api/v1/encounters/{encounters[0].id}/teacher-note-drafts",
        headers=auth_headers,
    )
    assert created.status_code == 201
    note_id = created.json()["note_id"]
    original_content = created.json()["content"]

    monkeypatch.setattr(
        "app.api.routes.notes.get_apprenticeship_writing_provider",
        lambda session: StubWritingProvider(invalid="attribution"),
    )
    rejected = client.post(f"/api/v1/notes/{note_id}/regenerate", headers=auth_headers)
    assert rejected.status_code == 502

    history = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers)
    versions = history.json()["versions"]
    assert len(versions) == 1
    assert versions[0]["content"] == original_content


def test_context_models_reject_extra_fields() -> None:
    from pydantic import ValidationError as PydanticValidationError

    from app.services.ai.writing import EvidenceRef

    with pytest.raises(PydanticValidationError):
        EvidenceRef(
            evidence_ref_id="r1",
            claim="claim",
            evidence_encounter_ids=["e1"],
            evidence_type="physician_record",
            evidence_keys=["k1"],
            invented_field="x",
        )


def test_global_note_library_lists_latest_edit_and_latest_final(
    client, auth_headers, db_session
) -> None:
    """GET /notes is the cross-patient writing library read model.

    A note finalized earlier must stay visible with its final version even
    after newer draft versions are appended on top of it: ``latest`` and
    ``latest_final`` are reported separately, never collapsed into one.
    """

    patient, encounters = _seed_writing_patient(db_session)

    original = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "case_record",
            "title": "脾虚两诊医案",
            "content": "我的原始整理。",
            "encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        },
    )
    assert original.status_code == 201, original.text
    note_id = original.json()["note_id"]

    draft = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={"content": "第一版草稿。", "final": False},
    )
    assert draft.status_code == 201, draft.text
    final = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={"content": "已定稿内容。", "final": True},
    )
    assert final.status_code == 201, final.text
    after_final_draft = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={"content": "定稿之后又写了新草稿。", "final": False},
    )
    assert after_final_draft.status_code == 201, after_final_draft.text

    # 患者此后新增一次就诊；作品必须提示“适用范围未包含”，而不是自动重写。
    extra_document = _document("extra-third-visit")
    db_session.add(
        Encounter(
            patient=patient,
            primary_document=extra_document,
            primary_document_id=extra_document.id,
            hospital="合成测试医院",
            encounter_at=datetime(2026, 8, 20, 9, 0),
            chief_complaint_raw="三诊",
            raw_source_type=SourceType.PHYSICIAN_RECORD,
        )
    )
    db_session.commit()

    library = client.get("/api/v1/notes", headers=auth_headers)
    assert library.status_code == 200, library.text
    body = library.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["note_id"] == note_id
    assert item["note_type"] == "case_record"
    assert item["version_count"] == 4
    assert item["patient"]["internal_patient_code"] == patient.internal_patient_code
    # 最近编辑版本：定稿之后的新草稿。
    assert item["latest"]["version"] == 4
    assert item["latest"]["version_type"] == "user_draft"
    # 最近定稿版本：不被新草稿顶掉。列表只加载摘要（无正文），正文按需读取。
    assert item["latest_final"] is not None
    assert item["latest_final"]["version"] == 3
    assert item["latest_final"]["version_type"] == "user_final"
    assert "content" not in item["latest_final"]
    assert "content" not in item["latest"]
    # 适用范围：依据截至 8-10 的两次就诊，其后还有 1 次未包含。
    assert item["evidence_window"]["later_encounter_count"] == 1
    assert item["evidence_window"]["latest_evidence_encounter_at"].startswith("2026-08-10")

    history = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers)
    assert history.status_code == 200, history.text
    assert history.json()["patient"]["internal_patient_code"] == patient.internal_patient_code
    assert history.json()["evidence_window"]["later_encounter_count"] == 1
    # 正文与历史版本按需在编辑页读取：定稿内容在这里完整可见。
    final_in_history = next(
        v for v in history.json()["versions"] if v["version"] == 3
    )
    assert final_in_history["content"].startswith("已定稿内容。")

    per_patient = client.get(f"/api/v1/patients/{patient.id}/notes", headers=auth_headers)
    assert per_patient.status_code == 200, per_patient.text
    per_patient_item = per_patient.json()["items"][0]
    assert per_patient_item["latest_final"]["version"] == 3
    assert per_patient_item["evidence_window"]["later_encounter_count"] == 1


def test_revision_inherits_base_version_evidence_not_latest_context(
    client, auth_headers, db_session
) -> None:
    """修订旧稿继承“所基于版本”的依据，不取最新版本、不按当前数据库重算。

    基础就诊数据在 v1 之后发生变化时，基于 v1 的修订必须仍携带 v1 的
    证据集合与上下文哈希；采用新资料必须走重新生成（显式重算）。
    """

    patient, encounters = _seed_writing_patient(db_session)
    original = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "case_record",
            "content": "我的原始整理。",
            "encounter_ids": [str(encounters[0].id)],
        },
    )
    assert original.status_code == 201, original.text
    note_id = original.json()["note_id"]
    v1_id = original.json()["id"]
    v1_hash = original.json()["source_context_hash"]

    # v1 之后底层数据发生变化（同一就诊的主诉被更新）。
    encounter = db_session.scalar(select(Encounter).where(Encounter.id == encounters[0].id))
    assert encounter is not None
    encounter.chief_complaint_raw = "复诊：症状已有变化"
    db_session.commit()

    revised = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={
            "content": "基于旧稿的修改。",
            "final": False,
            "based_on_version_id": v1_id,
        },
    )
    assert revised.status_code == 201, revised.text
    body = revised.json()
    # 继承所选版本的依据，而不是按变化后的数据重算。
    assert body["source_context_hash"] == v1_hash
    assert body["evidence_encounters"] == [str(encounters[0].id)]
    assert body["based_on_version_id"] == v1_id

    # 未指定基础版本时继承最新版本的依据（与 v1 相同的证据集合与哈希）。
    followup = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={"content": "继续修订。", "final": False},
    )
    assert followup.status_code == 201, followup.text
    assert followup.json()["source_context_hash"] == v1_hash

    history = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers)
    assert history.status_code == 200, history.text
    assert [item["source_context_hash"] for item in history.json()["versions"]] == [
        v1_hash,
        v1_hash,
        v1_hash,
    ]


def test_global_note_library_pagination_and_filters_resolve_in_the_database(
    client, auth_headers, db_session
) -> None:
    """作品库列表的筛选、排序、分页与计数都在数据库层完成。

    多部作品、多个版本时：limit/offset 只返回对应窗口；过滤器作用于“最新
    版本”；最新定稿查询与版本计数只覆盖当前页涉及的作品。列表请求的 SQL
    永远不把正文或 AI 结构化输出列从数据库读出（编辑页按需读取）。
    """

    patient, encounters = _seed_writing_patient(db_session)
    created_ids: list[str] = []
    for index in range(3):
        created = client.post(
            f"/api/v1/patients/{patient.id}/notes",
            headers=auth_headers,
            json={
                "note_type": "teacher_note" if index == 1 else "case_record",
                "content": f"原始整理 {index}。",
                "encounter_ids": [str(encounters[index % len(encounters)].id)],
            },
        )
        assert created.status_code == 201, created.text
        created_ids.append(created.json()["note_id"])
    # 最早的作品定稿；最新的作品在定稿之后又写了一版草稿。
    finalized = client.post(
        f"/api/v1/notes/{created_ids[0]}/revisions",
        headers=auth_headers,
        json={"content": "已定稿。", "final": True},
    )
    assert finalized.status_code == 201, finalized.text
    drafted = client.post(
        f"/api/v1/notes/{created_ids[2]}/revisions",
        headers=auth_headers,
        json={"content": "定稿之后的新草稿。", "final": False},
    )
    assert drafted.status_code == 201, drafted.text
    # 控制时间戳，让排序断言不依赖 SQLite 的秒级时间精度。
    for note_id, created_at in (
        (created_ids[0], datetime(2026, 9, 1, 8, 0)),
        (created_ids[1], datetime(2026, 9, 2, 8, 0)),
        (created_ids[2], datetime(2026, 9, 3, 8, 0)),
    ):
        db_session.execute(
            update(ApprenticeshipNoteVersion)
            .where(ApprenticeshipNoteVersion.note_id == uuid.UUID(note_id))
            .values(created_at=created_at)
        )
    db_session.commit()

    statements: list[str] = []

    def _capture(_conn, _cursor, statement, *_args) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _capture)
    try:
        page = client.get(
            "/api/v1/notes", headers=auth_headers, params={"limit": 2, "offset": 0}
        )
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    assert page.status_code == 200, page.text
    body = page.json()
    assert body["total"] == 3
    assert (body["limit"], body["offset"]) == (2, 0)
    # 最新编辑在前：C（9-3）→ B（9-2）；A（9-1）在下一页。
    assert [item["note_id"] for item in body["items"]] == [created_ids[2], created_ids[1]]
    assert body["items"][0]["version_count"] == 2
    assert body["items"][0]["latest"]["version"] == 2
    assert body["items"][0]["latest"]["version_type"] == "user_draft"
    assert body["items"][0]["latest_final"] is None
    assert body["items"][1]["version_count"] == 1
    assert "content" not in body["items"][0]["latest"]

    # 列表 SQL 从未读正文/结构化输出，且分页发生在数据库内。
    assert statements, "列表请求应产生数据库查询"
    assert any("LIMIT" in statement.upper() for statement in statements)
    for statement in statements:
        assert "content" not in statement, statement
        assert "structured_output" not in statement, statement

    offset_page = client.get(
        "/api/v1/notes", headers=auth_headers, params={"limit": 2, "offset": 2}
    )
    assert offset_page.status_code == 200, offset_page.text
    remaining = offset_page.json()
    assert remaining["total"] == 3
    assert [item["note_id"] for item in remaining["items"]] == [created_ids[0]]
    # 跨页作品的最新定稿仍随行：定稿不被后续草稿顶掉。
    assert remaining["items"][0]["latest"]["version_type"] == "user_final"
    assert remaining["items"][0]["latest_final"]["version"] == 2

    teachers = client.get(
        "/api/v1/notes", headers=auth_headers, params={"note_type": "teacher_note"}
    )
    assert teachers.status_code == 200, teachers.text
    assert teachers.json()["total"] == 1
    assert [item["note_id"] for item in teachers.json()["items"]] == [created_ids[1]]

    finals = client.get("/api/v1/notes", headers=auth_headers, params={"finalized": "true"})
    assert finals.status_code == 200, finals.text
    assert finals.json()["total"] == 1
    assert finals.json()["items"][0]["note_id"] == created_ids[0]
    in_progress = client.get(
        "/api/v1/notes", headers=auth_headers, params={"finalized": "false"}
    )
    assert in_progress.json()["total"] == 2

    scoped = client.get(
        "/api/v1/notes", headers=auth_headers, params={"patient_id": str(patient.id)}
    )
    assert scoped.json()["total"] == 3
    elsewhere = client.get(
        "/api/v1/notes", headers=auth_headers, params={"patient_id": str(uuid.uuid4())}
    )
    assert elsewhere.json()["total"] == 0
    assert elsewhere.json()["items"] == []


def test_get_note_version_by_id_supports_save_verification(
    client, auth_headers, db_session
) -> None:
    """GET /notes/versions/{id} answers “那次保存有没有入库？” by request id."""

    patient, encounters = _seed_writing_patient(db_session)
    created = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "case_record",
            "content": "原始稿。",
            "encounter_ids": [str(encounters[0].id)],
        },
    )
    assert created.status_code == 201, created.text
    request_id = str(uuid.uuid4())
    revision = client.post(
        f"/api/v1/notes/{created.json()['note_id']}/revisions",
        headers=auth_headers,
        json={
            "content": "未确认的保存。",
            "final": False,
            "based_on_version_id": created.json()["id"],
            "request_id": request_id,
        },
    )
    assert revision.status_code == 201, revision.text

    found = client.get(f"/api/v1/notes/versions/{request_id}", headers=auth_headers)
    assert found.status_code == 200, found.text
    assert found.json()["id"] == request_id
    # The read model re-attaches the system evidence appendix on presentation.
    assert found.json()["content"].startswith("未确认的保存。")
    assert "内容依据（Encounter IDs）：" in found.json()["content"]

    missing = client.get(f"/api/v1/notes/versions/{uuid.uuid4()}", headers=auth_headers)
    assert missing.status_code == 404

    anonymous = client.get(f"/api/v1/notes/versions/{request_id}")
    assert anonymous.status_code in (401, 403)


def test_evidence_preview_reports_set_diff_without_writing(
    client, auth_headers, db_session
) -> None:
    """依据预览：就诊集合增减与材料变化只读呈现，绝不创建版本。

    采用新资料的确认前提是先看到“拟新增/拟移除/材料是否变化”；预览命中
    无法唯一配对的处方对比时与采用路径同样 fail closed。跟师笔记固定一次
    就诊，预览同样拒绝多次。
    """

    patient, encounters = _seed_writing_patient(db_session)
    created = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "case_record",
            "content": "两诊整理。",
            "encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        },
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["note_id"]
    v1_hash = created.json()["source_context_hash"]

    # 患者此后新增第三次就诊，供扩展预览。
    extra_document = _document("preview-extra-visit")
    db_session.add(
        Encounter(
            patient=patient,
            primary_document=extra_document,
            primary_document_id=extra_document.id,
            hospital="合成测试医院",
            encounter_at=datetime(2026, 8, 25, 9, 0),
            chief_complaint_raw="四诊",
            raw_source_type=SourceType.PHYSICIAN_RECORD,
        )
    )
    db_session.commit()
    extra_id = str(
        db_session.scalar(
            select(Encounter.id)
            .where(Encounter.patient_id == patient.id)
            .order_by(Encounter.encounter_at.desc())
            .limit(1)
        )
    )

    same = client.post(
        f"/api/v1/notes/{note_id}/evidence-preview",
        headers=auth_headers,
        json={
            "based_on_version_id": created.json()["id"],
            "evidence_encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        },
    )
    assert same.status_code == 200, same.text
    body = same.json()
    # 预览绑定所基于的版本并回显，绝不偷偷换成最新版本。
    assert body["based_on_version"]["id"] == created.json()["id"]
    assert body["latest_version"]["id"] == created.json()["id"]
    assert len(body["proposal"]["preview_token"]) == 64
    assert body["set_changed"] is False
    assert body["materials_changed"] is False
    assert body["proposal"]["source_context_hash"] == v1_hash
    assert len(body["patient_encounters"]) == 3
    assert [row["chief_complaint_raw"] for row in body["patient_encounters"]] == [
        "腹胀两周",
        "腹胀减轻，仍乏力",
        "四诊",
    ]

    extended = client.post(
        f"/api/v1/notes/{note_id}/evidence-preview",
        headers=auth_headers,
        json={
            "based_on_version_id": created.json()["id"],
            "evidence_encounter_ids": [str(encounters[0].id), extra_id],
        },
    )
    assert extended.status_code == 200, extended.text
    extended_body = extended.json()
    assert extended_body["set_changed"] is True
    assert extended_body["materials_changed"] is True
    assert extended_body["proposal"]["added"] == [extra_id]
    assert extended_body["proposal"]["removed"] == [str(encounters[1].id)]

    # 重复勾选同一就诊：规范化后材料与去重请求完全一致（不按两次计算）。
    duplicated = client.post(
        f"/api/v1/notes/{note_id}/evidence-preview",
        headers=auth_headers,
        json={
            "based_on_version_id": created.json()["id"],
            "evidence_encounter_ids": [str(encounters[0].id), str(encounters[0].id)],
        },
    )
    assert duplicated.status_code == 200, duplicated.text
    duplicated_body = duplicated.json()
    assert duplicated_body["proposal"]["evidence_encounters"] == [str(encounters[0].id)]
    assert duplicated_body["proposal"]["source_context_hash"] == client.post(
        f"/api/v1/notes/{note_id}/evidence-preview",
        headers=auth_headers,
        json={
            "based_on_version_id": created.json()["id"],
            "evidence_encounter_ids": [str(encounters[0].id)],
        },
    ).json()["proposal"]["source_context_hash"]

    # 预览绝不落库：版本数保持 1。
    history = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers)
    assert len(history.json()["versions"]) == 1

    teacher = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "teacher_note",
            "content": "单诊笔记。",
            "encounter_ids": [str(encounters[0].id)],
        },
    )
    assert teacher.status_code == 201, teacher.text
    teacher_preview = client.post(
        f"/api/v1/notes/{teacher.json()['note_id']}/evidence-preview",
        headers=auth_headers,
        json={
            "based_on_version_id": teacher.json()["id"],
            "evidence_encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        },
    )
    assert teacher_preview.status_code == 422, teacher_preview.text

    # 预览所基于的版本必须属于该笔记：跨笔记版本一律 422。
    foreign_base = client.post(
        f"/api/v1/notes/{note_id}/evidence-preview",
        headers=auth_headers,
        json={
            "based_on_version_id": teacher.json()["id"],
            "evidence_encounter_ids": [str(encounters[0].id)],
        },
    )
    assert foreign_base.status_code == 422, foreign_base.text

    unknown = client.post(
        f"/api/v1/notes/{note_id}/evidence-preview",
        headers=auth_headers,
        json={
            "based_on_version_id": created.json()["id"],
            "evidence_encounter_ids": [str(uuid.uuid4())],
        },
    )
    assert unknown.status_code == 422, unknown.text


def test_preview_and_adopt_bind_to_the_viewed_version(
    client, auth_headers, db_session
) -> None:
    """查看旧版本而最新版本已前进：预览与采用始终基于所查看的版本。

    回归用户报告的断点：预览曾按最新版本计算增减，采用却按所选版本执行，
    两边数量可能对不上。这里让最新版本的依据与被查看版本不同，断言预览的
    增减只相对被查看版本，且采用生成的版本继承被查看版本的正文链。
    """

    patient, encounters = _seed_writing_patient(db_session)
    created = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "case_record",
            "content": "两诊整理。",
            "encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        },
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["note_id"]
    v1_id = created.json()["id"]

    # 新增第三次就诊，并把最新版本（v2）的依据扩到 {A, B, extra}。
    extra_document = _document("bind-extra-visit")
    db_session.add(
        Encounter(
            patient=patient,
            primary_document=extra_document,
            primary_document_id=extra_document.id,
            hospital="合成测试医院",
            encounter_at=datetime(2026, 8, 25, 9, 0),
            chief_complaint_raw="四诊",
            raw_source_type=SourceType.PHYSICIAN_RECORD,
        )
    )
    db_session.commit()
    extra_id = str(
        db_session.scalar(
            select(Encounter.id)
            .where(Encounter.patient_id == patient.id)
            .order_by(Encounter.encounter_at.desc())
            .limit(1)
        )
    )
    extend_preview = _preview_token(
        client,
        auth_headers,
        note_id,
        v1_id,
        [str(encounters[0].id), str(encounters[1].id), extra_id],
    )
    extended = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={
            "content": "三诊整理。",
            "final": False,
            "based_on_version_id": v1_id,
            "evidence_encounter_ids": [str(encounters[0].id), str(encounters[1].id), extra_id],
            "preview_token": extend_preview["proposal"]["preview_token"],
        },
    )
    assert extended.status_code == 201, extended.text
    v2_id = extended.json()["id"]

    # 用户回到 v1 查看，勾选只剩 {A}：预览必须相对 v1 的依据 {A,B} 计算，
    # 拟移除只有 B（若错按最新 v2 的 {A,B,extra} 计算，还会多出 extra）。
    stale_view = _preview_token(
        client, auth_headers, note_id, v1_id, [str(encounters[0].id)]
    )
    assert stale_view["based_on_version"]["id"] == v1_id
    assert stale_view["latest_version"]["id"] == v2_id
    assert stale_view["proposal"]["added"] == []
    assert stale_view["proposal"]["removed"] == [str(encounters[1].id)]
    # 入库依据按 ID 字符串序存储：按集合比较，与勾选顺序无关。
    assert sorted(stale_view["current"]["evidence_encounters"]) == sorted(
        [str(encounters[0].id), str(encounters[1].id)]
    )

    # 采用同样基于 v1：新版本依据是 {A}，挂在 v1 之下。
    adopted = client.post(
        f"/api/v1/notes/{note_id}/revisions",
        headers=auth_headers,
        json={
            "content": "回到首诊单独整理。",
            "final": False,
            "based_on_version_id": v1_id,
            "evidence_encounter_ids": [str(encounters[0].id)],
            "preview_token": stale_view["proposal"]["preview_token"],
        },
    )
    assert adopted.status_code == 201, adopted.text
    assert adopted.json()["based_on_version_id"] == v1_id
    assert adopted.json()["evidence_encounters"] == [str(encounters[0].id)]
    assert adopted.json()["source_context_hash"] == stale_view["proposal"]["source_context_hash"]

    history = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers).json()
    assert [row["version"] for row in history["versions"]] == [1, 2, 3]


def test_adopt_requires_fresh_preview_token_and_replays_skip_it(
    client, auth_headers, db_session
) -> None:
    """采用必须携带有效预览凭证；已入库请求的原样重放绕过凭证校验。

    - 无凭证 → 422（先预览再采用）。
    - 凭证与范围/版本不匹配 → 409。
    - 预览后材料变化（该就诊新增补录）→ 旧凭证 409，重新预览后可采用。
    - 已入库的采用在材料再次变化后原样重放 → 返回既有版本，不产生重复。
    """

    patient, encounters = _seed_writing_patient(db_session)
    created = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "case_record",
            "content": "两诊整理。",
            "encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        },
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["note_id"]
    v1_id = created.json()["id"]
    url = f"/api/v1/notes/{note_id}/revisions"

    def adopt_payload(token: str | None, request_id: str | None = None) -> dict:
        payload = {
            "content": "采用后的整理。",
            "final": False,
            "based_on_version_id": v1_id,
            "evidence_encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        }
        if token is not None:
            payload["preview_token"] = token
        if request_id is not None:
            payload["request_id"] = request_id
        return payload

    # 无凭证：先预览再采用是硬前提。
    missing = client.post(url, headers=auth_headers, json=adopt_payload(None))
    assert missing.status_code == 422, missing.text

    # 凭证错误（另一范围的预览）→ 409，不落任何版本。
    wrong_token = _preview_token(
        client, auth_headers, note_id, v1_id, [str(encounters[0].id)]
    )["proposal"]["preview_token"]
    mismatched = client.post(url, headers=auth_headers, json=adopt_payload(wrong_token))
    assert mismatched.status_code == 409, mismatched.text
    assert len(client.get(f"/api/v1/notes/{note_id}", headers=auth_headers).json()["versions"]) == 1

    # 预览 → 该就诊新增一条补录（材料已变）→ 旧凭证被拒绝。
    stale = _preview_token(
        client,
        auth_headers,
        note_id,
        v1_id,
        [str(encounters[0].id), str(encounters[1].id)],
    )
    added = client.post(
        f"/api/v1/encounters/{encounters[0].id}/student-notes",
        headers=auth_headers,
        json={
            "note_kind": "teacher_oral",
            "content": "预览之后才补录的老师口头信息",
            "request_id": str(uuid.uuid4()),
        },
    )
    assert added.status_code == 201, added.text
    outdated = client.post(
        url, headers=auth_headers, json=adopt_payload(stale["proposal"]["preview_token"])
    )
    assert outdated.status_code == 409, outdated.text

    # 重新预览拿到新凭证后可以采用；入库哈希与预览承诺一致。
    fresh = _preview_token(
        client,
        auth_headers,
        note_id,
        v1_id,
        [str(encounters[0].id), str(encounters[1].id)],
    )
    assert fresh["materials_changed"] is True
    request_id = str(uuid.uuid4())
    landed = client.post(
        url,
        headers=auth_headers,
        json=adopt_payload(fresh["proposal"]["preview_token"], request_id=request_id),
    )
    assert landed.status_code == 201, landed.text
    assert landed.json()["source_context_hash"] == fresh["proposal"]["source_context_hash"]

    # 材料再变一次，然后原样重放已入库的请求：凭证校验被幂等重放短路，
    # 返回既有版本，历史不再增长。
    added_again = client.post(
        f"/api/v1/encounters/{encounters[1].id}/student-notes",
        headers=auth_headers,
        json={
            "note_kind": "teacher_oral",
            "content": "采用入库之后的又一条补录",
            "request_id": str(uuid.uuid4()),
        },
    )
    assert added_again.status_code == 201, added_again.text
    replay = client.post(
        url,
        headers=auth_headers,
        json=adopt_payload(fresh["proposal"]["preview_token"], request_id=request_id),
    )
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == request_id
    history = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers).json()
    assert len(history["versions"]) == 2


def test_evidence_preview_breaks_material_changes_into_explained_parts(
    client, auth_headers, db_session
) -> None:
    """“材料变化”拆解：保留范围资料变化 / 规则版本变化 / 范围增减分列。

    旧版本没有快照、原范围不可解析、原范围命中歧义对比时如实降级说明，
    绝不把哈希不同笼统说成“资料变了”，也绝不回填历史快照。
    """

    patient, encounters = _seed_writing_patient(db_session)
    created = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "case_record",
            "content": "两诊整理。",
            "encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        },
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["note_id"]
    v1_id = created.json()["id"]

    def breakdown_for(ids: list[str]) -> dict:
        return _preview_token(client, auth_headers, note_id, v1_id, ids)["materials_breakdown"]

    # 数据未动：可比、保留范围资料未变、规则无差异。
    unchanged = breakdown_for([str(encounters[0].id), str(encounters[1].id)])
    assert unchanged == {
        "comparable": True,
        "not_comparable_reason": None,
        "kept_materials_changed": False,
        "rule_versions_changed": {},
    }

    # 规则版本变化单独呈现：篡改存储快照的上下文版本号（模拟旧规则时期
    # 的版本），资料未动——kept 仍为 False，规则差异被列出；哈希差异不
    # 会被笼统归因为“资料变化”。断言后恢复原值。
    row = db_session.get(ApprenticeshipNoteVersion, uuid.UUID(v1_id))
    assert row is not None and row.evidence_snapshot is not None
    original_snapshot = dict(row.evidence_snapshot)
    tampered = {**original_snapshot, "context_version": "writing-context-v0-legacy"}
    row.evidence_snapshot = tampered
    db_session.commit()
    ruled = breakdown_for([str(encounters[0].id), str(encounters[1].id)])
    assert ruled["comparable"] is True
    assert ruled["kept_materials_changed"] is False
    assert set(ruled["rule_versions_changed"]) == {"context_version"}
    assert ruled["rule_versions_changed"]["context_version"]["stored"] == (
        "writing-context-v0-legacy"
    )
    row.evidence_snapshot = original_snapshot
    db_session.commit()

    # 保留范围内的资料变化：给首次就诊补录一条学生记录 → kept=True。
    added = client.post(
        f"/api/v1/encounters/{encounters[0].id}/student-notes",
        headers=auth_headers,
        json={
            "note_kind": "teacher_oral",
            "content": "拆解测试的补录",
            "request_id": str(uuid.uuid4()),
        },
    )
    assert added.status_code == 201, added.text
    changed = breakdown_for([str(encounters[0].id), str(encounters[1].id)])
    assert changed["comparable"] is True
    assert changed["kept_materials_changed"] is True
    assert changed["rule_versions_changed"] == {}

    # 原范围不可解析：依据 ID 指向不存在的就诊（保持快照非 NULL）。
    row.evidence_snapshot = original_snapshot
    row.evidence_encounter_ids = [str(uuid.uuid4())]
    db_session.commit()
    unavailable = breakdown_for([str(encounters[0].id)])
    assert unavailable["comparable"] is False
    assert unavailable["not_comparable_reason"] == "base_set_unavailable"

    # 原范围命中歧义处方对比：首次就诊处方出现重复同一条目后，含两次就诊
    # 的原范围无法比较；范围只选第二次就诊时预览仍可用，但比较如实降级。
    row.evidence_encounter_ids = [str(encounters[0].id), str(encounters[1].id)]
    db_session.commit()
    first_prescription = db_session.scalar(
        select(Prescription).where(Prescription.encounter_id == encounters[0].id)
    )
    assert first_prescription is not None
    items = sorted(first_prescription.items, key=lambda item: item.line_number)
    template = items[0]
    first_prescription.items.append(
        PrescriptionItem(
            line_number=max(item.line_number for item in items) + 1,
            original_text=template.original_text,
            raw_drug_name=template.raw_drug_name,
            canonical_drug_name=template.canonical_drug_name,
            processing_method=template.processing_method,
            calculated_dose_g=template.calculated_dose_g,
            dose_source=template.dose_source,
            dose_scope=template.dose_scope,
            normalization_status=template.normalization_status,
            parser_version=template.parser_version,
        )
    )
    db_session.commit()
    # 拟采用范围包含歧义对时，预览整体 fail closed（既有行为）。
    blocked = client.post(
        f"/api/v1/notes/{note_id}/evidence-preview",
        headers=auth_headers,
        json={
            "based_on_version_id": v1_id,
            "evidence_encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        },
    )
    assert blocked.status_code == 409, blocked.text
    degraded = breakdown_for([str(encounters[1].id)])
    assert degraded["comparable"] is False
    assert degraded["not_comparable_reason"] == "base_ambiguous"

    # 旧版本未保存快照：明确说明无法比较，绝不按当前资料回填。
    row.evidence_snapshot = None
    db_session.commit()
    legacy = breakdown_for([str(encounters[1].id)])
    assert legacy == {
        "comparable": False,
        "not_comparable_reason": "legacy_no_snapshot",
        "kept_materials_changed": False,
        "rule_versions_changed": {},
    }


def _presented_like_content(body: str, evidence_ids: list[str]) -> str:
    """模拟客户端保存的正文：用户文字 + 系统维护的依据附录（尾部）。"""

    appendix_lines = ["――――", "内容依据（Encounter IDs）："]
    appendix_lines += [f"- {value}" for value in evidence_ids]
    return f"{body}\n\n" + "\n".join(appendix_lines)


def test_adopt_records_out_of_scope_reference_count_in_audit(
    client, auth_headers, db_session
) -> None:
    """采用新依据的确定性正文检查：被移除就诊仍被正文引用时审计留痕。

    计数只针对正文（剥离系统维护的依据附录后）：附录里的旧 ID 不算引用，
    正文提到被移除的就诊才算；“正文待核对”绝不阻断保存、绝不改写正文。
    """

    patient, encounters = _seed_writing_patient(db_session)
    created = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "case_record",
            "content": "两诊整理。",
            "encounter_ids": [str(encounters[0].id), str(encounters[1].id)],
        },
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["note_id"]
    v1_id = created.json()["id"]
    url = f"/api/v1/notes/{note_id}/revisions"

    def adopt(
        based_on_id: str,
        new_ids: list[str],
        body: str,
    ) -> dict:
        preview = _preview_token(client, auth_headers, note_id, based_on_id, new_ids)
        response = client.post(
            url,
            headers=auth_headers,
            json={
                "content": _presented_like_content(body, new_ids),
                "final": False,
                "based_on_version_id": based_on_id,
                "evidence_encounter_ids": new_ids,
                "preview_token": preview["proposal"]["preview_token"],
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    def audit_count(version_id: str) -> int:
        event = db_session.scalar(
            select(AuditEvent).where(
                AuditEvent.action == "note_version.revised",
                AuditEvent.entity_id == uuid.UUID(version_id),
            )
        )
        assert event is not None
        return event.after_redacted["removed_encounter_reference_count"]

    # 第一次采用：正文干净（被移除的 B 只出现在旧附录里）→ 计数 0。
    clean = adopt(
        v1_id,
        [str(encounters[0].id)],
        "只整理首诊，正文不提其他就诊。",
    )
    assert clean["evidence_encounters"] == [str(encounters[0].id)]
    assert audit_count(clean["id"]) == 0

    # 第二次采用：正文以大写形式提到被移除的 A——大小写不敏感，计数 1。
    removed_id = str(encounters[0].id)
    assert removed_id.upper() != removed_id, "测试 UUID 必须含 a–f，大写才有变化"
    referencing = adopt(
        clean["id"],
        [str(encounters[1].id)],
        f"正文以大写形式仍提到已移除的就诊 {removed_id.upper()}（第三诊叙述）。",
    )
    assert referencing["evidence_encounters"] == [str(encounters[1].id)]
    assert audit_count(referencing["id"]) == 1


def test_note_history_reports_adopted_material_changes(
    client, auth_headers, db_session
) -> None:
    """两类提示之二：已采用的资料发生变化 → matches_current_data=False。

    提示只表达“重新核对可用”，绝不把历史作品判成错误或过期；版本自身
    携带的哈希永远不被改写。
    """

    patient, encounters = _seed_writing_patient(db_session)
    created = client.post(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        json={
            "note_type": "case_record",
            "content": "原始整理。",
            "encounter_ids": [str(encounters[0].id)],
        },
    )
    assert created.status_code == 201, created.text
    note_id = created.json()["note_id"]
    v1_hash = created.json()["source_context_hash"]

    history = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers)
    assert history.status_code == 200, history.text
    assert history.json()["evidence_status"] == {
        "status": "ok",
        "matches_current_data": True,
    }

    encounter = db_session.scalar(select(Encounter).where(Encounter.id == encounters[0].id))
    assert encounter is not None
    encounter.chief_complaint_raw = "复诊：症状已有变化"
    db_session.commit()

    history = client.get(f"/api/v1/notes/{note_id}", headers=auth_headers)
    status = history.json()["evidence_status"]
    assert status == {"status": "ok", "matches_current_data": False}
    assert history.json()["versions"][0]["source_context_hash"] == v1_hash


def test_patient_notes_list_paginates_in_the_database(client, auth_headers, db_session):
    """P1-B：患者作品列表在数据库侧分页（total/limit/offset），条目语义不变。"""
    patient, encounters = _seed_writing_patient(db_session)
    for index in range(205):
        created = client.post(
            f"/api/v1/patients/{patient.id}/notes",
            headers=auth_headers,
            json={
                "note_type": "case_record",
                "content": f"分页基线作品 {index:03d}",
                "encounter_ids": [str(encounters[0].id)],
            },
        )
        assert created.status_code == 201, created.text

    default_page = client.get(
        f"/api/v1/patients/{patient.id}/notes", headers=auth_headers
    ).json()
    assert default_page["total"] == 205
    assert default_page["limit"] == 200
    assert default_page["offset"] == 0
    assert len(default_page["items"]) == 200

    # 排序语义不变：按作品首个版本创建时间升序，跨页不重不漏。
    first_ids = [item["note_id"] for item in default_page["items"]]
    assert len(set(first_ids)) == 200
    second_page = client.get(
        f"/api/v1/patients/{patient.id}/notes?offset=200", headers=auth_headers
    ).json()
    assert second_page["total"] == 205
    assert len(second_page["items"]) == 5
    assert not set(first_ids) & {item["note_id"] for item in second_page["items"]}

    # limit 上限与全局列表一致（200）。
    clamped = client.get(
        f"/api/v1/patients/{patient.id}/notes?limit=500", headers=auth_headers
    ).json()
    assert clamped["limit"] == 200
    assert len(clamped["items"]) == 200

    # 条目语义：latest 必有、version_count 如实、无定稿时 latest_final 为空。
    sample = default_page["items"][0]
    assert sample["latest"]["version"] == 1
    assert sample["version_count"] == 1
    assert sample["latest_final"] is None


def test_patient_notes_list_sort_and_filters(client, auth_headers, db_session):
    """sort=latest 与 note_type/encounter 过滤；version_count 不受过滤影响。"""
    patient, encounters = _seed_writing_patient(db_session)
    # 三篇：就诊 1 的医案（最早）、就诊 1 的医案（次早）、就诊 2 的跟师笔记（最晚）。
    created = []
    for note_type, encounter, content in (
        ("case_record", 0, "就诊一医案A"),
        ("case_record", 0, "就诊一医案B"),
        ("teacher_note", 1, "就诊二跟师笔记"),
    ):
        response = client.post(
            f"/api/v1/patients/{patient.id}/notes",
            headers=auth_headers,
            json={
                "note_type": note_type,
                "content": content,
                "encounter_ids": [str(encounters[encounter].id)],
            },
        )
        assert response.status_code == 201, response.text
        created.append(response.json())

    # This test checks ordering rather than the host clock's resolution. Windows
    # can give consecutive HTTP writes an identical timestamp; use distinct
    # synthetic timestamps so SQLite/PostgreSQL exercise the same ordering.
    for index, version in enumerate(created):
        db_session.execute(
            update(ApprenticeshipNoteVersion)
            .where(ApprenticeshipNoteVersion.id == uuid.UUID(version["id"]))
            .values(created_at=datetime.fromisoformat("2026-01-01T12:00:00+00:00").replace(second=index))
        )
    db_session.commit()

    # sort=latest：最新创建的排最前。
    latest = client.get(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        params={"sort": "latest"},
    ).json()
    # summary 投影不含正文：以版本 id 断言顺序。
    ids = [item["latest"]["id"] for item in latest["items"]]
    assert ids == [created[2]["id"], created[1]["id"], created[0]["id"]]

    # sort=first_created（默认）：保持原语义。
    first_created = client.get(
        f"/api/v1/patients/{patient.id}/notes", headers=auth_headers
    ).json()
    assert [item["latest"]["id"] for item in first_created["items"]] == [
        created[0]["id"],
        created[1]["id"],
        created[2]["id"],
    ]

    # note_type 过滤：只影响入选作品，version_count/latest 语义不变。
    cases = client.get(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        params={"note_type": "case_record"},
    ).json()
    assert cases["total"] == 2
    assert all(item["note_type"] == "case_record" for item in cases["items"])

    # encounter_id 过滤：按最新版本的依据就诊。
    by_encounter = client.get(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        params={"encounter_id": str(encounters[1].id)},
    ).json()
    assert by_encounter["total"] == 1
    assert by_encounter["items"][0]["latest"]["id"] == created[2]["id"]
    # 过滤组合 + 分页字段齐全。
    combined = client.get(
        f"/api/v1/patients/{patient.id}/notes",
        headers=auth_headers,
        params={
            "note_type": "case_record",
            "encounter_id": str(encounters[0].id),
            "sort": "latest",
            "limit": 1,
            "offset": 1,
        },
    ).json()
    assert combined["total"] == 2
    assert combined["limit"] == 1
    assert combined["offset"] == 1
    assert len(combined["items"]) == 1
    assert combined["items"][0]["latest"]["id"] == created[0]["id"]
    assert combined["items"][0]["version_count"] == 1

    # 非法 sort 显式 422。
    bad = client.get(
        f"/api/v1/patients/{patient.id}/notes", headers=auth_headers, params={"sort": "bogus"}
    )
    assert bad.status_code == 422
