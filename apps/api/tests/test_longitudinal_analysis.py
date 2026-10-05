from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.models.domain import (
    AIAnalysis,
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
    AITaskType,
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
from app.services.ai.longitudinal import (
    LongitudinalAnalysisRejected,
    LongitudinalContextBuilder,
    validate_longitudinal_output,
)

FIXTURE = Path(__file__).parent / "fixtures" / "patient_longitudinal_analysis.json"


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


def _seed_longitudinal_patient(db_session) -> tuple[Patient, list[Encounter]]:
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
                    parser_version="synthetic-longitudinal-v1",
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


def _make_ref(
    context: dict[str, Any], evidence_ref_id: str, keys: list[str], claim: str
) -> dict[str, Any]:
    entries = [context["evidence_catalog"][key] for key in keys]
    encounter_ids = sorted(
        {encounter_id for entry in entries for encounter_id in entry["encounter_ids"]}
    )
    sources = {entry["evidence_type"] for entry in entries}
    evidence_type = next(iter(sources)) if len(sources) == 1 else "mixed"
    return {
        "evidence_ref_id": evidence_ref_id,
        "claim": claim,
        "evidence_encounter_ids": encounter_ids,
        "evidence_type": evidence_type,
        "evidence_keys": keys,
    }


def _valid_output(context: dict[str, Any]) -> dict[str, Any]:
    encounters = context["encounters"]
    first, second, third = encounters
    first_sleep = next(item for item in first["symptoms"] if item["normalized"] == "睡眠")
    second_sleep = next(item for item in second["symptoms"] if item["normalized"] == "睡眠")
    diagnosis_keys = [encounter["diagnoses"][0]["evidence_key"] for encounter in encounters]
    summary_keys = [encounter["recorded_fields"][0]["evidence_key"] for encounter in encounters]
    diff_one = second["prescription_diff_from_previous"]
    diff_two = third["prescription_diff_from_previous"]
    increased_key = next(
        item["evidence_key"]
        for item in diff_one["items"]
        if item["canonical_identity"] == "白术"
    )
    unknown_key = next(
        item["evidence_key"]
        for item in diff_two["items"]
        if item["canonical_identity"] == "半夏"
    )
    refs = [
        _make_ref(context, "summary", summary_keys, "三次就诊的主诉记录"),
        _make_ref(context, "sleep_first", [first_sleep["evidence_key"]], "首诊睡眠差"),
        _make_ref(context, "sleep_second", [second_sleep["evidence_key"]], "二诊睡眠改善"),
        _make_ref(
            context,
            "sleep_missing",
            [f"encounters.{third['encounter_id']}.symptoms.index"],
            "三诊未记录睡眠",
        ),
        _make_ref(context, "diagnoses", diagnosis_keys, "证候记录"),
        _make_ref(context, "dose_increased", [increased_key], "白术加量"),
        _make_ref(context, "dose_unknown", [unknown_key], "半夏剂量变化未知"),
    ]
    persistent_refs: list[str] = []
    persistent_ref_by_name: dict[str, str] = {}
    for index, medication in enumerate(context["deterministic_persistent_medications"]):
        ref_id = f"persistent_{index}"
        refs.append(
            _make_ref(
                context,
                ref_id,
                medication["evidence_keys"],
                f"{medication['name']}在所选范围持续出现",
            )
        )
        persistent_refs.append(ref_id)
        persistent_ref_by_name[medication["name"]] = ref_id
    persistent = [
        {
            "medication": medication["name"],
            "encounter_ids": medication["encounter_ids"],
            "evidence_ref_ids": [persistent_refs[index]],
        }
        for index, medication in enumerate(context["deterministic_persistent_medications"])
    ]
    return {
        "course_summary": {
            "text": "睡眠在二诊记录为改善，腹胀至三诊记录为减轻。",
            "evidence_ref_ids": ["summary"],
        },
        "symptom_trajectory": [
            {
                "symptom": "睡眠",
                "points": [
                    {
                        "encounter_id": first["encounter_id"],
                        "date": first["date"],
                        "state": "new",
                        "description": "睡眠差。",
                        "evidence_ref_ids": ["sleep_first"],
                    },
                    {
                        "encounter_id": second["encounter_id"],
                        "date": second["date"],
                        "state": "improved",
                        "description": "睡眠较前改善。",
                        "evidence_ref_ids": ["sleep_second"],
                    },
                    {
                        "encounter_id": third["encounter_id"],
                        "date": third["date"],
                        "state": "not_recorded",
                        "description": "第三诊未记录睡眠情况。",
                        "evidence_ref_ids": ["sleep_missing"],
                    },
                ],
            }
        ],
        "diagnosis_trajectory": [
            {
                "diagnosis": "脾气虚",
                "points": [
                    {
                        "encounter_id": encounter["encounter_id"],
                        "date": encounter["date"],
                        "state": "recorded",
                        "description": "记录为脾气虚。",
                        "evidence_ref_ids": ["diagnoses"],
                    }
                    for encounter in encounters
                ],
            }
        ],
        "prescription_strategy_trajectory": [
            {
                "previous_encounter_id": first["encounter_id"],
                "current_encounter_id": second["encounter_id"],
                "summary": "白术由10g增至15g。",
                "deterministic_changes": ["DOSE_INCREASED"],
                "evidence_ref_ids": ["dose_increased"],
            },
            {
                "previous_encounter_id": second["encounter_id"],
                "current_encounter_id": third["encounter_id"],
                "summary": "半夏剂量变化无法确定。",
                "deterministic_changes": ["UNKNOWN"],
                "evidence_ref_ids": ["dose_unknown"],
            },
        ],
        "persistent_medications": persistent,
        "key_turning_points": [
            {
                "previous_encounter_id": first["encounter_id"],
                "current_encounter_id": second["encounter_id"],
                "recorded_changes": ["睡眠较前改善", "白术剂量增加"],
                "evidence_ref_ids": ["sleep_second", "dose_increased"],
            }
        ],
        "possible_clinical_reasoning": [
            {
                "inference": (
                    "从记录看，本次调整可能与持续腹胀及睡眠变化同时相关，"
                    "但记录未明确说明原因。"
                ),
                "evidence_ref_ids": ["sleep_second", "dose_increased"],
                "provenance": "ai_inferred",
                "tentative": True,
            }
        ],
        "uncertainty_and_missing_data": [
            {
                "text": "第三诊未记录睡眠情况，不能据此判断睡眠稳定或症状消失。",
                "evidence_ref_ids": ["sleep_missing"],
            },
            {
                "text": "半夏剂量变化无法确定。",
                "evidence_ref_ids": ["dose_unknown"],
            },
        ],
        "discussion_questions": [
            {
                "question": "睡眠改善后继续保留酸枣仁的主要考虑是什么？",
                "evidence_ref_ids": [persistent_ref_by_name["酸枣仁"]],
            }
        ],
        "evidence_refs": refs,
    }


class StubLongitudinalProvider:
    def __init__(self, *, invalid_diff: bool = False) -> None:
        self.contexts: list[dict[str, Any]] = []
        self.invalid_diff = invalid_diff

    def run(
        self, *, prompt: str, context: dict[str, Any], schema: dict[str, Any]
    ) -> AIProviderResult:
        self.contexts.append(context)
        output = _valid_output(context)
        if self.invalid_diff:
            output["prescription_strategy_trajectory"][0]["deterministic_changes"] = [
                "DOSE_DECREASED"
            ]
        return AIProviderResult(
            provider="stub",
            model="strong-text-test",
            output=json.dumps(output, ensure_ascii=False),
            structured_output=output,
        )


def test_context_is_ordered_deidentified_and_consumes_phase7_diff(db_session) -> None:
    patient, encounters = _seed_longitudinal_patient(db_session)
    context = LongitudinalContextBuilder().build(
        patient,
        encounters,
        ["测试姓名", "SYN-PRIVATE-001"],
        deidentification_version="test-v1",
    )
    payload = context.model_dump(mode="json")

    assert [item["date"] for item in payload["encounters"]] == [
        "2026-08-01",
        "2026-08-08",
        "2026-08-15",
    ]
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "测试姓名" not in serialized
    assert "SYN-PRIVATE-001" not in serialized
    assert payload["encounters"][0]["symptoms"][0]["source"] == "physician_record"
    assert payload["encounters"][1]["symptoms"][0]["source"] == "ai_extracted"
    assert payload["encounters"][2]["student_notes"][0]["source"] == "student_note"
    diff = payload["encounters"][1]["prescription_diff_from_previous"]
    assert diff["algorithm_version"] == "1.0.0"
    assert any(item["dose_change"] == "UNKNOWN" for item in diff["items"])
    assert all("evidence_key" in item for item in diff["items"])


def test_strict_validation_rejects_diff_recalculation_and_untraceable_evidence(
    db_session,
) -> None:
    patient, encounters = _seed_longitudinal_patient(db_session)
    context = LongitudinalContextBuilder().build(
        patient, encounters, [], deidentification_version="test-v1"
    )
    output = _valid_output(context.model_dump(mode="json"))
    output["prescription_strategy_trajectory"][0]["deterministic_changes"] = [
        "DOSE_DECREASED"
    ]
    with pytest.raises(LongitudinalAnalysisRejected):
        validate_longitudinal_output(output, context)

    output = _valid_output(context.model_dump(mode="json"))
    output["evidence_refs"][0]["evidence_keys"] = ["invented.fact"]
    with pytest.raises(LongitudinalAnalysisRejected):
        validate_longitudinal_output(output, context)


def test_api_persists_append_only_versions_and_exposes_patient_history(
    client, auth_headers, db_session, monkeypatch
) -> None:
    patient, encounters = _seed_longitudinal_patient(db_session)
    stub = StubLongitudinalProvider()
    monkeypatch.setattr(
        "app.api.routes.analyses.get_longitudinal_analysis_provider",
        lambda session: stub,
    )

    first = client.post(
        f"/api/v1/patients/{patient.id}/longitudinal-analyses",
        headers=auth_headers,
        json={},
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/api/v1/patients/{patient.id}/longitudinal-analyses",
        headers=auth_headers,
        json={
            "start_encounter_id": str(encounters[0].id),
            "end_encounter_id": str(encounters[2].id),
        },
    )
    assert second.status_code == 201, second.text
    assert first.json()["id"] != second.json()["id"]
    assert first.json()["source"] == "ai_inferred"
    assert first.json()["source_encounters"] == [str(item.id) for item in encounters]
    assert first.json()["structured_output"]["symptom_trajectory"][0]["points"][2][
        "state"
    ] == "not_recorded"
    assert "测试姓名" not in json.dumps(stub.contexts, ensure_ascii=False)

    history = client.get(
        f"/api/v1/patients/{patient.id}/longitudinal-analyses", headers=auth_headers
    )
    assert history.status_code == 200
    assert len(history.json()["items"]) == 2
    assert db_session.scalar(
        select(func.count(AIAnalysis.id)).where(
            AIAnalysis.task_type == AITaskType.PATIENT_LONGITUDINAL_ANALYSIS
        )
    ) == 2


def test_api_fails_closed_without_provider_or_with_invalid_output(
    client, auth_headers, db_session, monkeypatch
) -> None:
    patient, _ = _seed_longitudinal_patient(db_session)
    monkeypatch.setattr(
        "app.api.routes.analyses.get_longitudinal_analysis_provider",
        lambda session: None,
    )
    missing_provider = client.post(
        f"/api/v1/patients/{patient.id}/longitudinal-analyses",
        headers=auth_headers,
        json={},
    )
    assert missing_provider.status_code == 503

    monkeypatch.setattr(
        "app.api.routes.analyses.get_longitudinal_analysis_provider",
        lambda session: StubLongitudinalProvider(invalid_diff=True),
    )
    invalid = client.post(
        f"/api/v1/patients/{patient.id}/longitudinal-analyses",
        headers=auth_headers,
        json={},
    )
    assert invalid.status_code == 502
    assert db_session.scalar(select(func.count(AIAnalysis.id))) == 0
