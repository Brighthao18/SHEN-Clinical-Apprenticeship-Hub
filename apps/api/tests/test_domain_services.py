from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select, text

from app.models.domain import (
    Document,
    HospitalDrugSpecification,
    Patient,
    PatientIdentity,
)
from app.models.enums import DocumentType, ProcessingStatus
from app.services.drugs import (
    calculate_course_total_dose,
    calculate_dose,
    find_effective_specification,
    normalize_drug,
)
from app.services.parsers.jnu import JNUHospitalOutpatientParser
from app.services.patient_matching import PatientMatcher

FIXTURE = Path(__file__).parent / "fixtures" / "jnu_outpatient_v1.txt"


def _document() -> Document:
    return Document(
        original_filename="fixture.jpg",
        original_file_path="aa/bb/hash.jpg",
        sha256="a" * 64,
        byte_size=10,
        mime_type="image/jpeg",
        document_type=DocumentType.OUTPATIENT_RECORD,
        processing_status=ProcessingStatus.UPLOADED,
    )


def test_drug_normalization_and_dose_calculation(db_session) -> None:
    result = normalize_drug(db_session, "醋鳖甲", None)
    assert result.canonical_name == "鳖甲"
    assert result.processing_method == "醋制"
    assert result.dictionary is not None
    assert calculate_dose(Decimal("10"), Decimal("2")) == Decimal("20")
    assert calculate_dose(None, Decimal("2")) is None
    assert calculate_course_total_dose(Decimal("20"), 7) == Decimal("140")
    assert calculate_course_total_dose(Decimal("20"), None) is None


def test_only_manual_verified_hospital_specification_can_derive_dose(db_session) -> None:
    normalized = normalize_drug(db_session, "鳖甲", None)
    assert normalized.dictionary is not None
    legacy = HospitalDrugSpecification(
        hospital="暨南大学附属第一医院（广州华侨医院）",
        drug_dictionary_id=normalized.dictionary.id,
        processing_method=None,
        package_dose_g=Decimal("9"),
        normalized_package_unit="package",
        effective_from=None,
        effective_to=None,
        source_type="legacy_unverified",
        source_note="migration placeholder",
        version="1",
    )
    verified = HospitalDrugSpecification(
        hospital="暨南大学附属第一医院（广州华侨医院）",
        drug_dictionary_id=normalized.dictionary.id,
        processing_method=None,
        package_dose_g=Decimal("10"),
        normalized_package_unit="package",
        effective_from=None,
        effective_to=None,
        source_type="manual_verified",
        source_note="user confirmed",
        version="2",
    )
    db_session.add_all([legacy, verified])
    db_session.commit()

    specification = find_effective_specification(
        db_session,
        hospital="暨南大学附属第一医院（广州华侨医院）",
        dictionary_id=normalized.dictionary.id,
        processing_method=None,
        normalized_package_unit="package",
        encounter_date=date(2026, 8, 29),
    )

    assert specification is not None
    assert specification.id == verified.id
    assert calculate_dose(None, Decimal("2"), specification) == Decimal("20")


def test_patient_matching_uses_hospital_and_mrn_and_encrypts_pii(db_session) -> None:
    document = _document()
    db_session.add(document)
    db_session.flush()
    parsed = JNUHospitalOutpatientParser().parse(FIXTURE.read_text(encoding="utf-8"))

    first = PatientMatcher(db_session).match_or_create(parsed, evidence_document_id=document.id)
    second = PatientMatcher(db_session).match_or_create(parsed, evidence_document_id=document.id)
    db_session.commit()

    assert first.created is True
    assert second.created is False
    assert first.patient.id == second.patient.id
    assert first.patient.internal_patient_code == "P000001"
    assert db_session.scalar(select(Patient).where(Patient.id == first.patient.id)) is not None
    identity = db_session.scalar(select(PatientIdentity))
    assert identity is not None and identity.real_name == "张三"

    raw_name = db_session.execute(text("SELECT real_name FROM patient_identities")).scalar_one()
    raw_mrn = db_session.execute(
        text("SELECT hospital_medical_record_number FROM patient_identities")
    ).scalar_one()
    assert "张三" not in raw_name
    assert "JNU-000128" not in raw_mrn
    assert parsed.encounter_date == date(2026, 8, 14)
