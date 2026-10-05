"""Drug dictionary management route tests.

The dictionary is the only canonical-identity authority: PENDING candidates
require user verification, DISABLED entries never resolve, merges fold raw
names into a survivor, and manual_verified hospital specifications attach
only to VERIFIED entries.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.domain import (
    Document,
    DrugDictionary,
    Encounter,
    HospitalDrugSpecification,
    Patient,
    Prescription,
    PrescriptionItem,
)
from app.models.enums import DocumentType as DocumentTypeEnum
from app.models.enums import ProcessingStatus, Sex
from app.models.enums import SourceType as SourceTypeEnum


def _candidate(db_session, raw_name: str, canonical: str | None = None) -> DrugDictionary:
    entry = DrugDictionary(
        canonical_name=canonical or raw_name,
        processing_method=None,
        candidate_raw_name=raw_name,
        aliases=[],
        hospital_display_names=[raw_name],
        common_ocr_errors=[],
        dictionary_version=get_settings().drug_dictionary_version,
        effective_from=date(2026, 1, 1),
        source="test",
        status="PENDING",
    )
    db_session.add(entry)
    db_session.commit()
    return entry


def test_list_filters_reject_unknown_status_and_search(
    client: TestClient, auth_headers, db_session
) -> None:
    bad = client.get(
        "/api/v1/drug-dictionary?status=disabledx", headers=auth_headers
    )
    assert bad.status_code == 422

    entry = _candidate(db_session, "威灵仙")
    response = client.get(
        "/api/v1/drug-dictionary?q=威灵&status=PENDING", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    assert any(row["id"] == str(entry.id) for row in response.json())

    empty = client.get("/api/v1/drug-dictionary?q=不存在的药名xyz", headers=auth_headers)
    assert empty.status_code == 200
    assert empty.json() == []


def test_verify_rejects_unknown_and_verifies_candidate(
    client: TestClient, auth_headers, db_session
) -> None:
    missing = client.post(
        f"/api/v1/drug-dictionary/{uuid.uuid4()}/verify",
        headers=auth_headers,
        json={"canonical_name": "威灵仙"},
    )
    assert missing.status_code == 404

    entry = _candidate(db_session, "威灵仙")
    response = client.post(
        f"/api/v1/drug-dictionary/{entry.id}/verify",
        headers=auth_headers,
        json={"canonical_name": "威灵仙", "processing_method": None},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verified"] is True
    assert body["canonical_name"] == "威灵仙"
    assert body["backfilled_items"] == 0
    db_session.expire_all()
    assert db_session.get(DrugDictionary, entry.id).status == "VERIFIED"


def test_batch_verify_reports_per_id_errors(
    client: TestClient, auth_headers, db_session
) -> None:
    entry = _candidate(db_session, "徐长卿")
    missing_id = uuid.uuid4()
    response = client.post(
        "/api/v1/drug-dictionary/batch-verify",
        headers=auth_headers,
        json={"ids": [str(entry.id), str(missing_id)]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verified"] == 1
    assert body["errors"] == [f"{missing_id}: not found"]
    db_session.expire_all()
    assert db_session.get(DrugDictionary, entry.id).status == "VERIFIED"


def test_disable_restore_cycle(client: TestClient, auth_headers, db_session) -> None:
    entry = _candidate(db_session, "络石藤")

    disabled = client.post(
        f"/api/v1/drug-dictionary/{entry.id}/disable", headers=auth_headers
    )
    assert disabled.status_code == 200, disabled.text
    db_session.expire_all()
    assert db_session.get(DrugDictionary, entry.id).status == "DISABLED"

    # A disabled entry refuses verification until it is restored.
    blocked = client.post(
        f"/api/v1/drug-dictionary/{entry.id}/verify",
        headers=auth_headers,
        json={"canonical_name": "络石藤"},
    )
    assert blocked.status_code == 422
    assert "restored" in blocked.json()["detail"]

    restored = client.post(
        f"/api/v1/drug-dictionary/{entry.id}/restore", headers=auth_headers
    )
    assert restored.status_code == 200, restored.text
    db_session.expire_all()
    assert db_session.get(DrugDictionary, entry.id).status == "PENDING"

    missing = client.post(
        f"/api/v1/drug-dictionary/{uuid.uuid4()}/disable", headers=auth_headers
    )
    assert missing.status_code == 404


def test_merge_folds_source_into_target_and_rejects_bad_targets(
    client: TestClient, auth_headers, db_session
) -> None:
    source = _candidate(db_session, "银花")
    target = _candidate(db_session, "金银花")
    target.status = "VERIFIED"
    db_session.commit()

    self_merge = client.post(
        f"/api/v1/drug-dictionary/{source.id}/merge",
        headers=auth_headers,
        json={"target_id": str(source.id)},
    )
    assert self_merge.status_code == 422
    assert "itself" in self_merge.json()["detail"]

    response = client.post(
        f"/api/v1/drug-dictionary/{source.id}/merge",
        headers=auth_headers,
        json={"target_id": str(target.id)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["merged"] is True
    db_session.expire_all()
    source_row = db_session.get(DrugDictionary, source.id)
    merged = db_session.get(DrugDictionary, target.id)
    assert source_row.status == "DISABLED"
    # The survivor keeps its own raw name; the source's raw name is released
    # and its identity fields fold into the target.
    assert merged.candidate_raw_name == "金银花"
    assert source_row.candidate_raw_name is None

    missing = client.post(
        f"/api/v1/drug-dictionary/{uuid.uuid4()}/merge",
        headers=auth_headers,
        json={"target_id": str(target.id)},
    )
    assert missing.status_code == 404


def test_patch_updates_identity_fields_and_audits(
    client: TestClient, auth_headers, db_session
) -> None:
    entry = _candidate(db_session, "徐长卿", canonical="徐长卿")
    response = client.patch(
        f"/api/v1/drug-dictionary/{entry.id}",
        headers=auth_headers,
        json={
            "canonical_name": "徐长卿 ",
            "aliases": ["寥刁竹", ""],
            "hospital_display_names": ["徐长卿"],
        },
    )
    assert response.status_code == 200, response.text
    db_session.expire_all()
    patched = db_session.get(DrugDictionary, entry.id)
    assert patched.canonical_name == "徐长卿"
    assert patched.aliases == ["寥刁竹"]

    missing = client.patch(
        f"/api/v1/drug-dictionary/{uuid.uuid4()}",
        headers=auth_headers,
        json={"canonical_name": "不存在"},
    )
    assert missing.status_code == 404


def test_specifications_require_verified_entry_and_stay_unique(
    client: TestClient, auth_headers, db_session
) -> None:
    entry = _candidate(db_session, "红景天")

    payload = {
        "drug_dictionary_id": str(entry.id),
        "hospital": "暨南大学附属第一医院",
        "package_dose_g": "10",
        "original_package_unit": "包",
        "processing_method": None,
        "effective_from": "2026-01-01",
        "source_note": "药房标签",
    }

    on_missing = client.post(
        "/api/v1/drug-dictionary/specifications",
        headers=auth_headers,
        json={**payload, "drug_dictionary_id": str(uuid.uuid4())},
    )
    assert on_missing.status_code == 404

    on_pending = client.post(
        "/api/v1/drug-dictionary/specifications", headers=auth_headers, json=payload
    )
    assert on_pending.status_code == 422
    assert "VERIFIED" in on_pending.json()["detail"]

    entry.status = "VERIFIED"
    db_session.commit()

    created = client.post(
        "/api/v1/drug-dictionary/specifications", headers=auth_headers, json=payload
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["normalized_package_unit"] == "package"
    assert body["package_dose_g"] == "10"
    assert body["source_type"] == "manual_verified"

    duplicate = client.post(
        "/api/v1/drug-dictionary/specifications", headers=auth_headers, json=payload
    )
    assert duplicate.status_code == 409

    bad_unit = client.post(
        "/api/v1/drug-dictionary/specifications",
        headers=auth_headers,
        json={**payload, "original_package_unit": "克"},
    )
    assert bad_unit.status_code == 422

    with SessionLocal() as session:
        specs = session.scalars(
            select(HospitalDrugSpecification).where(
                HospitalDrugSpecification.drug_dictionary_id == entry.id
            )
        ).all()
        assert len(specs) == 1
        assert specs[0].package_dose_g == Decimal("10")


def test_list_reports_prescription_impact_counts(
    client: TestClient, auth_headers, db_session
) -> None:
    """维护影响范围：linked = 已解析处方条目；pending_raw = 同名可回填条目。"""

    import hashlib

    verified = _candidate(db_session, "陈皮", canonical="陈皮")
    verified.status = "VERIFIED"
    pending = _candidate(db_session, "茯神")

    digest = hashlib.sha256(b"impact-count-doc").hexdigest()
    document = Document(
        id=uuid.uuid4(),
        original_filename="impact.jpg",
        original_file_path=f"synthetic/{digest}.jpg",
        sha256=digest,
        byte_size=10,
        mime_type="image/jpeg",
        document_type=DocumentTypeEnum.OUTPATIENT_RECORD,
        processing_status=ProcessingStatus.ACCEPTED,
    )
    patient = Patient(internal_patient_code="P000099", sex=Sex.FEMALE)
    encounter = Encounter(
        patient=patient,
        primary_document=document,
        primary_document_id=document.id,
        hospital="合成测试医院",
        encounter_at=date(2026, 9, 1),
    )
    prescription = Prescription(
        encounter=encounter,
        evidence_document_id=document.id,
        total_decoctions=7,
        source_type=SourceTypeEnum.PHYSICIAN_RECORD,
    )
    prescription.items.append(
        PrescriptionItem(
            line_number=1,
            original_text="陈皮 6g",
            raw_drug_name="陈皮",
            drug_dictionary_id=verified.id,
            canonical_drug_name="陈皮",
            normalization_status="normalized",
            parser_version="test-v1",
        )
    )
    prescription.items.append(
        PrescriptionItem(
            line_number=2,
            original_text="茯神 10g",
            raw_drug_name="茯神",
            normalization_status="pending",
            parser_version="test-v1",
        )
    )
    db_session.add_all([document, patient, encounter, prescription])
    db_session.commit()

    response = client.get("/api/v1/drug-dictionary", headers=auth_headers)
    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()}
    assert rows[str(verified.id)]["linked_item_count"] == 1
    assert rows[str(pending.id)]["pending_raw_item_count"] == 1
