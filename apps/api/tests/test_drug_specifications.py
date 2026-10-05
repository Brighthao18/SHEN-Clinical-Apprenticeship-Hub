"""Manual hospital drug specification supplementation.

Covers the ``POST /drug-dictionary/specifications`` endpoint (the only
sanctioned write path for ``manual_verified`` reference specifications,
used from the compatibility review page) and the review-list encounter
context that prefills the supplement form.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.domain import (
    AuditEvent,
    Document,
    DrugDictionary,
    Encounter,
    HospitalDrugSpecification,
    Patient,
    ReviewItem,
)
from app.models.enums import DocumentType, ProcessingStatus, Sex
from app.services.drugs import find_effective_specification


def _seed_entry(status: str = "VERIFIED", processing: str | None = None) -> DrugDictionary:
    with SessionLocal() as session:
        entry = DrugDictionary(
            canonical_name="金银花",
            candidate_raw_name=None if status == "VERIFIED" else "金银花饮片",
            processing_method=processing,
            aliases=[],
            hospital_display_names=[],
            common_ocr_errors=[],
            dictionary_version=get_settings().drug_dictionary_version,
            effective_from=date(2026, 1, 1),
            source="test seed",
            status=status,
        )
        session.add(entry)
        session.commit()
        return entry


def _payload(entry_id: uuid.UUID, **overrides) -> dict:
    payload = {
        "drug_dictionary_id": str(entry_id),
        "hospital": "暨南大学附属第一医院",
        "package_dose_g": "10",
        "original_package_unit": "包",
        "processing_method": None,
        "effective_from": "2026-08-01",
        "effective_to": None,
        "source_note": "药房标签",
    }
    payload.update(overrides)
    return payload


def test_specification_endpoint_requires_auth(client) -> None:
    response = client.post(
        "/api/v1/drug-dictionary/specifications",
        json={
            "drug_dictionary_id": str(uuid.uuid4()),
            "hospital": "x",
            "package_dose_g": "1",
            "original_package_unit": "包",
        },
    )
    assert response.status_code == 401


def test_create_manual_specification_normalizes_and_is_effective(
    client, auth_headers, db_session
) -> None:
    entry = _seed_entry()
    response = client.post(
        "/api/v1/drug-dictionary/specifications",
        headers=auth_headers,
        json=_payload(entry.id, processing_method="醋"),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    # 包 normalizes to the stored ``package`` unit and 醋 to the processing
    # vocabulary used at ingestion, so pipeline lookups match.
    assert body["normalized_package_unit"] == "package"
    assert body["processing_method"] == "醋制"
    assert body["source_type"] == "manual_verified"
    assert body["version"] == get_settings().drug_dictionary_version
    assert Decimal(body["package_dose_g"]) == Decimal("10")

    with SessionLocal() as session:
        stored = session.scalar(select(HospitalDrugSpecification))
        assert stored is not None
        assert stored.package_dose_g == Decimal("10.000")
        audit_count = session.scalar(
            select(func.count(AuditEvent.id)).where(
                AuditEvent.action == "drug_specification.created"
            )
        )
        assert audit_count == 1

    # The pipeline's spec lookup must now resolve a dose for this drug.
    found = find_effective_specification(
        db_session,
        hospital="暨南大学附属第一医院",
        dictionary_id=entry.id,
        processing_method="醋制",
        normalized_package_unit="package",
        encounter_date=date(2026, 8, 30),
    )
    assert found is not None
    assert found.package_dose_g == Decimal("10.000")


def test_create_rejects_unknown_unit_negative_dose_and_unverified_entry(
    client, auth_headers
) -> None:
    entry = _seed_entry()

    response = client.post(
        "/api/v1/drug-dictionary/specifications",
        headers=auth_headers,
        json=_payload(entry.id, original_package_unit="盒"),
    )
    assert response.status_code == 422

    response = client.post(
        "/api/v1/drug-dictionary/specifications",
        headers=auth_headers,
        json=_payload(entry.id, package_dose_g="0"),
    )
    assert response.status_code == 422

    response = client.post(
        "/api/v1/drug-dictionary/specifications",
        headers=auth_headers,
        json=_payload(uuid.uuid4()),
    )
    assert response.status_code == 404

    pending = _seed_entry(status="PENDING")
    response = client.post(
        "/api/v1/drug-dictionary/specifications",
        headers=auth_headers,
        json=_payload(pending.id),
    )
    assert response.status_code == 422


def test_duplicate_version_is_rejected_but_new_effective_period_is_allowed(
    client, auth_headers
) -> None:
    entry = _seed_entry()
    first = client.post(
        "/api/v1/drug-dictionary/specifications", headers=auth_headers, json=_payload(entry.id)
    )
    assert first.status_code == 201

    duplicate = client.post(
        "/api/v1/drug-dictionary/specifications", headers=auth_headers, json=_payload(entry.id)
    )
    assert duplicate.status_code == 409

    # A different effective period is a legitimate new reference row.
    updated = client.post(
        "/api/v1/drug-dictionary/specifications",
        headers=auth_headers,
        json=_payload(entry.id, effective_from="2026-09-01", package_dose_g="12"),
    )
    assert updated.status_code == 201, updated.text

    with SessionLocal() as session:
        assert (
            session.scalar(select(func.count(HospitalDrugSpecification.id))) == 2
        )


def _seed_review_chain() -> uuid.UUID:
    with SessionLocal() as session:
        patient = Patient(
            internal_patient_code="P000001",
            sex=Sex.FEMALE,
        )
        session.add(patient)
        session.flush()
        document = Document(
            original_filename="case.jpg",
            original_file_path="data/storage/case.jpg",
            sha256=uuid.uuid4().hex,
            byte_size=10,
            mime_type="image/jpeg",
            document_type=DocumentType.PRESCRIPTION_SLIP,
            processing_status=ProcessingStatus.ACCEPTED,
        )
        session.add(document)
        session.flush()
        encounter = Encounter(
            patient_id=patient.id,
            primary_document_id=document.id,
            hospital="暨南大学附属第一医院",
            encounter_at=datetime(2026, 8, 29, 10, 30),
        )
        session.add(encounter)
        session.flush()
        review = ReviewItem(
            document_id=document.id,
            encounter_id=encounter.id,
            issue_code="HOSPITAL_SPECIFICATION_UNKNOWN",
            field_path="prescription.items[0].calculated_dose_g",
            blocking=False,
            observed_value={"drug": "金银花", "package_count": "2"},
        )
        session.add(review)
        session.commit()
        return review.id


def test_review_list_carries_hospital_and_encounter_date(client, auth_headers) -> None:
    review_id = _seed_review_chain()
    response = client.get("/api/v1/reviews?status=open", headers=auth_headers)
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()}
    assert str(review_id) in rows
    row = rows[str(review_id)]
    assert row["hospital"] == "暨南大学附属第一医院"
    assert row["encounter_date"] == "2026-08-29"
    assert row["observed_value"]["drug"] == "金银花"
