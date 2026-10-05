from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.types import pii_fingerprint
from app.models.domain import Patient, PatientIdentity, SystemCounter
from app.models.enums import Sex
from app.services.parsers.jnu import CANONICAL_HOSPITAL
from app.services.parsers.types import ParsedDocument


class PatientMatchReview(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PatientMatchResult:
    patient: Patient
    identity: PatientIdentity
    created: bool


def normalize_hospital(value: str) -> str:
    compact = "".join(value.split())
    if "暨南大学附属第一医院" in compact or "广州华侨医院" in compact:
        return CANONICAL_HOSPITAL
    return value.strip()


class PatientMatcher:
    def __init__(self, session: Session) -> None:
        self.session = session

    def match_or_create(
        self, parsed: ParsedDocument, *, evidence_document_id: uuid.UUID
    ) -> PatientMatchResult:
        if not parsed.medical_record_number:
            raise PatientMatchReview(
                "MEDICAL_RECORD_NUMBER_MISSING",
                "No stable medical record number was extracted; fuzzy candidates require review.",
            )
        hospital = normalize_hospital(parsed.hospital)
        mrn_fingerprint = pii_fingerprint(parsed.medical_record_number)
        identity = self.session.scalar(
            select(PatientIdentity).where(
                PatientIdentity.hospital == hospital,
                PatientIdentity.mrn_fingerprint == mrn_fingerprint,
            )
        )
        if identity is not None:
            patient = identity.patient
            name_fingerprint = pii_fingerprint(parsed.real_name)
            if (
                name_fingerprint
                and identity.real_name_fingerprint
                and name_fingerprint != identity.real_name_fingerprint
            ):
                raise PatientMatchReview(
                    "PATIENT_IDENTITY_NAME_CONFLICT",
                    "Stable medical record number matched but the extracted name conflicts.",
                )
            if patient.sex not in {Sex.UNKNOWN, parsed.sex} and parsed.sex != Sex.UNKNOWN:
                raise PatientMatchReview(
                    "PATIENT_IDENTITY_SEX_CONFLICT",
                    "Stable medical record number matched but sex conflicts.",
                )
            if patient.sex == Sex.UNKNOWN and parsed.sex != Sex.UNKNOWN:
                patient.sex = parsed.sex
                patient.row_version += 1
            return PatientMatchResult(patient=patient, identity=identity, created=False)

        code = self._next_patient_code()
        estimated_year = (
            parsed.encounter_date.year - parsed.age
            if parsed.encounter_date is not None
            and parsed.age is not None
            and 0 <= parsed.age <= 130
            else None
        )
        patient = Patient(
            internal_patient_code=code,
            sex=parsed.sex,
            estimated_birth_year=estimated_year,
        )
        self.session.add(patient)
        self.session.flush()
        identity = PatientIdentity(
            patient_id=patient.id,
            hospital=hospital,
            real_name=parsed.real_name,
            hospital_medical_record_number=parsed.medical_record_number,
            real_name_fingerprint=pii_fingerprint(parsed.real_name),
            mrn_fingerprint=mrn_fingerprint,
            evidence_document_id=evidence_document_id,
        )
        self.session.add(identity)
        self.session.flush()
        return PatientMatchResult(patient=patient, identity=identity, created=True)

    def _next_patient_code(self) -> str:
        counter = self.session.scalar(
            select(SystemCounter).where(SystemCounter.name == "patient_code").with_for_update()
        )
        if counter is None:
            counter = SystemCounter(name="patient_code", value=0)
            self.session.add(counter)
            self.session.flush()
        counter.value += 1
        return f"P{counter.value:06d}"
