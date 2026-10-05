from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.models.domain import Document, Encounter, Patient, Prescription, PrescriptionItem
from app.models.enums import (
    DocumentType,
    DoseScope,
    DoseSource,
    ProcessingStatus,
    Sex,
)

FIXTURE = Path(__file__).parent / "fixtures" / "prescription_diff_longitudinal.json"


def _document(seed: str) -> Document:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return Document(
        id=uuid.uuid4(),
        original_filename=f"{seed}.jpg",
        original_file_path=f"synthetic/{digest}.jpg",
        sha256=digest,
        byte_size=10,
        mime_type="image/jpeg",
        document_type=DocumentType.OUTPATIENT_RECORD,
        processing_status=ProcessingStatus.ACCEPTED,
    )


def _encounter(
    patient: Patient,
    *,
    label: str,
    encounter_at: str,
    items: list[dict[str, Any]],
) -> Encounter:
    document = _document(label)
    encounter = Encounter(
        patient=patient,
        primary_document=document,
        primary_document_id=document.id,
        hospital="合成测试医院",
        encounter_at=datetime.fromisoformat(encounter_at),
    )
    prescription = Prescription(
        encounter=encounter,
        evidence_document_id=document.id,
        total_decoctions=7,
    )
    for line_number, payload in enumerate(items, start=1):
        dose = payload["calculated_dose_g"]
        prescription.items.append(
            PrescriptionItem(
                line_number=line_number,
                original_text=f"{payload['raw_drug_name']} synthetic evidence",
                raw_drug_name=payload["raw_drug_name"],
                canonical_drug_name=payload["canonical_drug_name"],
                processing_method=payload["processing_method"],
                calculated_dose_g=Decimal(dose) if dose is not None else None,
                dose_source=DoseSource.CALCULATED if dose is not None else DoseSource.UNKNOWN,
                dose_scope=DoseScope(payload["dose_scope"]),
                normalization_status=payload["normalization_status"],
                parser_version="synthetic-diff-v1",
            )
        )
    encounter.prescriptions.append(prescription)
    return encounter


def _seed_pair(db_session) -> tuple[Encounter, Encounter]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    patient = Patient(internal_patient_code=payload["patient_code"], sex=Sex.UNKNOWN)
    previous = _encounter(patient, label="previous", **payload["previous"])
    current = _encounter(patient, label="current", **payload["current"])
    db_session.add_all([patient, previous, current])
    db_session.commit()
    return previous, current


def _by_identity(payload: dict[str, Any], identity: str) -> dict[str, Any]:
    return next(item for item in payload["items"] if item["canonical_identity"] == identity)


def test_diff_api_covers_all_required_deterministic_changes(
    client, auth_headers, db_session
) -> None:
    previous, current = _seed_pair(db_session)

    response = client.get(
        f"/api/v1/encounters/{current.id}/prescription-diff",
        headers=auth_headers,
        params={"compare_to_encounter_id": str(previous.id)},
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["algorithm_version"] == "1.0.0"
    assert payload["dose_basis"] == "per_decoction"
    assert payload["previous_encounter"]["id"] == str(previous.id)
    assert payload["current_encounter"]["id"] == str(current.id)
    assert payload["summary"] == {
        "total": 8,
        "added": 1,
        "removed": 1,
        "dose_increased": 2,
        "dose_decreased": 1,
        "processing_changed": 1,
        "unchanged": 2,
        "normalization_pending": 1,
        "unknown_dose": 1,
    }

    white_atractylodes = _by_identity(payload, "白术")
    assert white_atractylodes["primary_change"] == "PROCESSING_CHANGED"
    assert white_atractylodes["previous_raw_name"] == "白术"
    assert white_atractylodes["current_raw_name"] == "炒白术"
    assert white_atractylodes["previous_processing"] is None
    assert white_atractylodes["current_processing"] == "炒制"

    astragalus = _by_identity(payload, "黄芪")
    assert astragalus["dose_change"] == "INCREASED"
    assert astragalus["previous_dose_g"] == "15"
    assert astragalus["current_dose_g"] == "20"
    assert astragalus["signed_change_g"] == "5"
    assert astragalus["absolute_change_g"] == "5"

    licorice = _by_identity(payload, "甘草")
    assert licorice["dose_change"] == "DECREASED"
    assert licorice["signed_change_g"] == "-3"
    assert licorice["absolute_change_g"] == "3"

    pinellia = _by_identity(payload, "半夏")
    assert pinellia["primary_change"] == "UNCHANGED"
    assert pinellia["dose_change"] == "UNKNOWN"
    assert pinellia["absolute_change_g"] is None

    pending = next(item for item in payload["items"] if item["current_raw_name"] == "夜交藤")
    assert pending["identity_basis"] == "raw_exact"
    assert pending["canonical_identity"] is None
    assert pending["change_types"] == ["DOSE_INCREASED", "NORMALIZATION_PENDING"]
    assert pending["evidence"]["previous_encounter_id"] == str(previous.id)
    assert pending["evidence"]["current_encounter_id"] == str(current.id)


def test_diff_api_finds_previous_encounter_automatically_and_orders_explicit_pair(
    client, auth_headers, db_session
) -> None:
    previous, current = _seed_pair(db_session)

    automatic = client.get(
        f"/api/v1/encounters/{current.id}/prescription-diff", headers=auth_headers
    )
    assert automatic.status_code == 200, automatic.text
    assert automatic.json()["previous_encounter"]["id"] == str(previous.id)

    reversed_pair = client.get(
        f"/api/v1/encounters/{previous.id}/prescription-diff",
        headers=auth_headers,
        params={"compare_to_encounter_id": str(current.id)},
    )
    assert reversed_pair.status_code == 200, reversed_pair.text
    assert reversed_pair.json() == automatic.json()

    no_previous = client.get(
        f"/api/v1/encounters/{previous.id}/prescription-diff", headers=auth_headers
    )
    assert no_previous.status_code == 404


def test_pending_raw_identity_must_match_exactly(client, auth_headers, db_session) -> None:
    patient = Patient(internal_patient_code="P900002", sex=Sex.UNKNOWN)
    previous = _encounter(
        patient,
        label="pending-previous",
        encounter_at="2026-08-01T00:00:00+00:00",
        items=[
            {
                "raw_drug_name": "川贝母",
                "canonical_drug_name": None,
                "processing_method": None,
                "calculated_dose_g": "6",
                "dose_scope": "per_decoction",
                "normalization_status": "pending",
            }
        ],
    )
    current = _encounter(
        patient,
        label="pending-current",
        encounter_at="2026-08-08T00:00:00+00:00",
        items=[
            {
                "raw_drug_name": "川贝",
                "canonical_drug_name": None,
                "processing_method": None,
                "calculated_dose_g": "6",
                "dose_scope": "per_decoction",
                "normalization_status": "pending",
            }
        ],
    )
    db_session.add_all([patient, previous, current])
    db_session.commit()

    response = client.get(
        f"/api/v1/encounters/{current.id}/prescription-diff",
        headers=auth_headers,
        params={"compare_to_encounter_id": str(previous.id)},
    )
    assert response.status_code == 200, response.text
    assert [item["primary_change"] for item in response.json()["items"]] == [
        "ADDED",
        "REMOVED",
    ]
    assert response.json()["summary"]["normalization_pending"] == 2


def test_ambiguous_duplicate_identity_fails_closed(client, auth_headers, db_session) -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    patient = Patient(internal_patient_code="P900003", sex=Sex.UNKNOWN)
    duplicate = payload["previous"]["items"][0]
    previous = _encounter(
        patient,
        label="duplicates-previous",
        encounter_at="2026-08-01T00:00:00+00:00",
        items=[duplicate, duplicate],
    )
    current_item = dict(duplicate)
    current_item["processing_method"] = "炒制"
    current_item["raw_drug_name"] = "炒白术"
    current = _encounter(
        patient,
        label="duplicates-current",
        encounter_at="2026-08-08T00:00:00+00:00",
        items=[current_item],
    )
    db_session.add_all([patient, previous, current])
    db_session.commit()

    response = client.get(
        f"/api/v1/encounters/{current.id}/prescription-diff",
        headers=auth_headers,
        params={"compare_to_encounter_id": str(previous.id)},
    )
    assert response.status_code == 409
    assert "停止比较" in response.json()["detail"]


def test_course_total_or_unknown_scope_is_never_used_for_dose_change(
    client, auth_headers, db_session
) -> None:
    patient = Patient(internal_patient_code="P900004", sex=Sex.UNKNOWN)
    item = {
        "raw_drug_name": "黄芪",
        "canonical_drug_name": "黄芪",
        "processing_method": None,
        "calculated_dose_g": "70",
        "dose_scope": "total_prescription",
        "normalization_status": "normalized",
    }
    previous = _encounter(
        patient,
        label="course-total-previous",
        encounter_at="2026-08-01T00:00:00+00:00",
        items=[item],
    )
    current_item = dict(item)
    current_item["calculated_dose_g"] = "140"
    current = _encounter(
        patient,
        label="course-total-current",
        encounter_at="2026-08-08T00:00:00+00:00",
        items=[current_item],
    )
    db_session.add_all([patient, previous, current])
    db_session.commit()

    response = client.get(
        f"/api/v1/encounters/{current.id}/prescription-diff",
        headers=auth_headers,
        params={"compare_to_encounter_id": str(previous.id)},
    )
    assert response.status_code == 200, response.text
    diff_item = response.json()["items"][0]
    assert diff_item["dose_change"] == "UNKNOWN"
    assert diff_item["previous_dose_g"] is None
    assert diff_item["current_dose_g"] is None
    assert diff_item["signed_change_g"] is None
    assert diff_item["absolute_change_g"] is None


def test_cross_patient_comparison_is_rejected(client, auth_headers, db_session) -> None:
    first_patient = Patient(internal_patient_code="P900005", sex=Sex.UNKNOWN)
    second_patient = Patient(internal_patient_code="P900006", sex=Sex.UNKNOWN)
    item = json.loads(FIXTURE.read_text(encoding="utf-8"))["previous"]["items"][:1]
    first = _encounter(
        first_patient,
        label="cross-first",
        encounter_at="2026-08-01T00:00:00+00:00",
        items=item,
    )
    second = _encounter(
        second_patient,
        label="cross-second",
        encounter_at="2026-08-08T00:00:00+00:00",
        items=item,
    )
    db_session.add_all([first_patient, second_patient, first, second])
    db_session.commit()

    response = client.get(
        f"/api/v1/encounters/{second.id}/prescription-diff",
        headers=auth_headers,
        params={"compare_to_encounter_id": str(first.id)},
    )
    assert response.status_code == 422
