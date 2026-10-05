from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.core.security import CurrentUser
from app.db.session import get_db
from app.db.types import pii_fingerprint
from app.models.domain import (
    AIAnalysis,
    Encounter,
    Patient,
    PatientIdentity,
    Prescription,
    PrescriptionItem,
    StudentNote,
)
from app.schemas.api import StudentNoteCreate, StudentNoteRead

router = APIRouter(tags=["patients"])
DbSession = Annotated[Session, Depends(get_db)]


def _masked_name(value: str | None) -> str | None:
    if not value:
        return None
    return f"{value[0]}某"


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _patient_summary(patient: Patient, encounter_count: int | None = None) -> dict[str, Any]:
    identity = patient.identities[0] if patient.identities else None
    return {
        "id": str(patient.id),
        "internal_patient_code": patient.internal_patient_code,
        "display_name": _masked_name(identity.real_name) if identity else None,
        "sex": patient.sex.value,
        "estimated_birth_year": patient.estimated_birth_year,
        "first_encounter_at": patient.first_encounter_at,
        "last_encounter_at": patient.last_encounter_at,
        "encounter_count": (
            encounter_count if encounter_count is not None else len(patient.encounters)
        ),
    }


def _serialize_encounter(encounter: Encounter) -> dict[str, Any]:
    """Full read model of one encounter; shared by the patient detail and the
    single-encounter endpoint."""
    prescriptions = []
    for prescription in encounter.prescriptions:
        items = []
        for item in prescription.items:
            display_dose = None
            if item.calculated_dose_g is not None:
                suffix = "〔规格推算〕" if item.dose_source.value == "hospital_spec" else ""
                display_dose = f"{_decimal(item.calculated_dose_g)}g{suffix}"
            items.append(
                {
                    "id": str(item.id),
                    "original_text": item.original_text,
                    "is_basic_drug": item.is_basic_drug,
                    "raw_drug_name": item.raw_drug_name,
                    "canonical_drug_name": item.canonical_drug_name,
                    "processing_method": item.processing_method,
                    "explicit_package_dose_g": _decimal(item.explicit_package_dose_g),
                    "package_count": _decimal(item.package_count),
                    "original_package_unit": item.original_package_unit,
                    "normalized_package_unit": item.normalized_package_unit,
                    "calculated_dose_g": _decimal(item.calculated_dose_g),
                    "display_dose": display_dose or "未记录",
                    "dose_source": item.dose_source.value,
                    "dose_scope": item.dose_scope.value,
                    "confidence": _decimal(item.confidence),
                    "review_status": item.review_status.value,
                    # Items carry no provenance column of their own; the
                    # prescription's source (physician_record for the
                    # legacy path, ai_normalized for AI structuring) is
                    # the item's source too.
                    "source": prescription.source_type.value,
                }
            )
        prescriptions.append(
            {
                "id": str(prescription.id),
                "total_decoctions": prescription.total_decoctions,
                "raw_instruction_text": prescription.raw_instruction_text,
                "source": prescription.source_type.value,
                "items": items,
            }
        )

    return {
        "id": str(encounter.id),
        "encounter_at": encounter.encounter_at,
        "hospital": encounter.hospital,
        "department": encounter.department,
        "physician": encounter.physician,
        "age_at_encounter": encounter.age_at_encounter,
        "chief_complaint_raw": encounter.chief_complaint_raw,
        "present_illness_raw": encounter.present_illness_raw,
        "past_history_raw": encounter.past_history_raw,
        "allergy_raw": encounter.allergy_raw,
        "source": encounter.raw_source_type.value,
        "document_id": str(encounter.primary_document_id),
        "document_url": f"/api/v1/documents/{encounter.primary_document_id}/content",
        "symptoms": [
            {
                "id": str(symptom.id),
                "raw": symptom.symptom_raw,
                "normalized": symptom.symptom_normalized,
                "status": symptom.status.value,
                "trend": symptom.trend.value if symptom.trend else None,
                "source": symptom.source_type.value,
            }
            for symptom in encounter.symptoms
        ],
        "diagnoses": [
            {
                "id": str(diagnosis.id),
                "raw": diagnosis.name_raw,
                "normalized": diagnosis.name_normalized,
                "diagnosis_type": diagnosis.diagnosis_type.value,
                "is_primary": diagnosis.is_primary,
                "source": diagnosis.source_type.value,
            }
            for diagnosis in encounter.diagnoses
        ],
        "prescriptions": prescriptions,
        "student_notes": [
            {
                "id": str(note.id),
                "note_kind": note.note_kind.value,
                "content": note.content,
                "source": note.source_type.value,
                "version": note.version,
            }
            for note in encounter.student_notes
        ],
        "ai_analyses": [
            {
                "id": str(analysis.id),
                "task_type": analysis.task_type.value,
                "provider": analysis.provider,
                "model": analysis.model,
                "output": analysis.output,
                "created_at": analysis.created_at,
                "source": "ai_inferred",
            }
            for analysis in encounter.analyses
        ],
    }


def _encounter_summaries(db: Session, patient_id: uuid.UUID) -> list[dict[str, Any]]:
    """Light per-encounter rows for the workspace date index.

    Column projection plus three grouped count queries — no encounter text
    payloads, prescriptions or notes are loaded here.
    """

    encounter_rows = db.execute(
        select(
            Encounter.id,
            Encounter.encounter_at,
            Encounter.hospital,
            Encounter.department,
            Encounter.physician,
            Encounter.chief_complaint_raw,
            Encounter.raw_source_type,
        )
        .where(Encounter.patient_id == patient_id)
        .order_by(Encounter.encounter_at.desc())
    ).all()
    encounter_ids = [row.id for row in encounter_rows]
    item_counts: dict[uuid.UUID, int] = {}
    note_counts: dict[uuid.UUID, int] = {}
    analysis_counts: dict[uuid.UUID, int] = {}
    if encounter_ids:
        item_counts = dict(
            db.execute(
                select(Prescription.encounter_id, func.count(PrescriptionItem.id))
                .join(PrescriptionItem, PrescriptionItem.prescription_id == Prescription.id)
                .where(Prescription.encounter_id.in_(encounter_ids))
                .group_by(Prescription.encounter_id)
            ).all()
        )
        note_counts = dict(
            db.execute(
                select(StudentNote.encounter_id, func.count(StudentNote.id))
                .where(StudentNote.encounter_id.in_(encounter_ids))
                .group_by(StudentNote.encounter_id)
            ).all()
        )
        analysis_counts = dict(
            db.execute(
                select(AIAnalysis.encounter_id, func.count(AIAnalysis.id))
                .where(AIAnalysis.encounter_id.in_(encounter_ids))
                .group_by(AIAnalysis.encounter_id)
            ).all()
        )
    return [
        {
            "id": str(row.id),
            "encounter_at": row.encounter_at,
            "hospital": row.hospital,
            "department": row.department,
            "physician": row.physician,
            "chief_complaint_raw": row.chief_complaint_raw,
            "source": row.raw_source_type.value,
            "prescription_item_count": item_counts.get(row.id, 0),
            "student_note_count": note_counts.get(row.id, 0),
            "ai_analysis_count": analysis_counts.get(row.id, 0),
        }
        for row in encounter_rows
    ]


@router.get("/patients")
def list_patients(
    user: CurrentUser,
    db: DbSession,
    query: Annotated[str | None, Query(max_length=80)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    filtered = select(Patient.id)
    if query:
        fingerprint = pii_fingerprint(query)
        filtered = (
            filtered.outerjoin(PatientIdentity)
            .where(
                or_(
                    Patient.internal_patient_code.ilike(f"%{query}%"),
                    PatientIdentity.real_name_fingerprint == fingerprint,
                )
            )
            .distinct()
        )
    total = db.scalar(select(func.count()).select_from(filtered.subquery()))
    patients = db.scalars(
        select(Patient)
        .where(Patient.id.in_(filtered))
        .options(selectinload(Patient.identities), selectinload(Patient.encounters))
        .order_by(Patient.last_encounter_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return {
        "items": [_patient_summary(patient) for patient in patients],
        "limit": limit,
        "offset": offset,
        "total": total or 0,
    }


@router.get("/patients/{patient_id}")
def get_patient(
    patient_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    view: Annotated[str | None, Query(pattern="^(full|summary)$")] = None,
) -> dict[str, Any]:
    if view == "summary":
        # 工作区日期索引用：患者头部 + 轻量就诊摘要，不装载就诊正文。
        patient = db.scalar(
            select(Patient)
            .where(Patient.id == patient_id)
            .options(selectinload(Patient.identities))
        )
        if patient is None:
            raise HTTPException(status_code=404, detail="Patient not found")
        summaries = _encounter_summaries(db, patient_id)
        result = _patient_summary(patient, encounter_count=len(summaries))
        result["encounters"] = summaries
        result["view"] = "summary"
        return result

    statement = (
        select(Patient)
        .where(Patient.id == patient_id)
        .options(
            selectinload(Patient.identities),
            selectinload(Patient.encounters).selectinload(Encounter.symptoms),
            selectinload(Patient.encounters).selectinload(Encounter.diagnoses),
            selectinload(Patient.encounters)
            .selectinload(Encounter.prescriptions)
            .selectinload(Prescription.items),
            selectinload(Patient.encounters).selectinload(Encounter.student_notes),
            selectinload(Patient.encounters).selectinload(Encounter.analyses),
        )
    )
    patient = db.scalar(statement)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")

    ordered = sorted(patient.encounters, key=lambda item: item.encounter_at, reverse=True)
    encounters = [_serialize_encounter(encounter) for encounter in ordered]
    result = _patient_summary(patient)
    result["encounters"] = encounters
    return result


@router.get("/encounters/{encounter_id}/owner")
def get_encounter_owner(
    encounter_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    """Resolve ONE encounter's patient ownership — positioning only.

    The workbench's local 补录 records carry just an encounter id; this
    authenticated lookup lets them build an exact deep link without exposing
    any clinical content. 404 means the encounter does not exist (deleted or
    wrong id): the caller must say so instead of guessing an owner.
    """

    patient_id = db.scalar(
        select(Encounter.patient_id).where(Encounter.id == encounter_id)
    )
    if patient_id is None:
        raise HTTPException(status_code=404, detail="Encounter not found")
    return {"encounter_id": str(encounter_id), "patient_id": str(patient_id)}


@router.get("/patients/{patient_id}/encounters/{encounter_id}")
def get_patient_encounter(
    patient_id: uuid.UUID,
    encounter_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    """Full content of ONE encounter; the workspace loads these on demand."""

    encounter = db.scalar(
        select(Encounter)
        .where(Encounter.id == encounter_id, Encounter.patient_id == patient_id)
        .options(
            selectinload(Encounter.symptoms),
            selectinload(Encounter.diagnoses),
            selectinload(Encounter.prescriptions).selectinload(Prescription.items),
            selectinload(Encounter.student_notes),
            selectinload(Encounter.analyses),
        )
    )
    if encounter is None:
        raise HTTPException(status_code=404, detail="Encounter not found")
    return _serialize_encounter(encounter)


@router.get(
    "/encounters/{encounter_id}/student-notes/{note_id}",
    response_model=StudentNoteRead,
)
def get_student_note(
    encounter_id: uuid.UUID,
    note_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> StudentNote:
    """Resolve ONE student note by id for save-result verification.

    A retryable save stores its ``request_id`` as the note row id; the client
    looks it up here before deciding to replay an uncertain save.
    """

    note = db.get(StudentNote, note_id)
    if note is None or note.encounter_id != encounter_id:
        raise HTTPException(status_code=404, detail="Student note not found")
    return note


@router.post(
    "/encounters/{encounter_id}/student-notes",
    response_model=StudentNoteRead,
    status_code=status.HTTP_201_CREATED,
)
def create_student_note(
    encounter_id: uuid.UUID,
    payload: StudentNoteCreate,
    user: CurrentUser,
    db: DbSession,
) -> StudentNote:
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status_code=404, detail="Encounter not found")

    def replay() -> StudentNote | None:
        existing = db.get(StudentNote, payload.request_id) if payload.request_id else None
        if existing is not None and (
            existing.encounter_id != encounter_id
            or existing.note_kind != payload.note_kind
            or existing.content != payload.content
        ):
            raise HTTPException(status_code=409, detail="保存标识已用于其他内容，请核对后重新提交")
        return existing

    existing = replay()
    if existing is not None:
        return existing
    note = StudentNote(
        id=payload.request_id or uuid.uuid4(),
        patient_id=encounter.patient_id,
        encounter_id=encounter.id,
        note_kind=payload.note_kind,
        content=payload.content,
    )
    db.add(note)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = replay()
        if existing is not None:
            return existing
        raise
    db.refresh(note)
    return note
