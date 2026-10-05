from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.core.security import CurrentUser
from app.db.session import get_db
from app.models.domain import (
    AIAnalysis,
    Document,
    Encounter,
    ImportItem,
    Patient,
    ReviewItem,
    StudentNote,
)
from app.models.enums import ProcessingStatus, ReviewStatus

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("")
def dashboard(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    settings = get_settings()
    zone = ZoneInfo(settings.app_timezone)
    local_now = datetime.now(zone)
    week_start_local = datetime.combine(
        (local_now - timedelta(days=local_now.weekday())).date(), time.min, tzinfo=zone
    )
    week_start = week_start_local.astimezone(UTC)

    imported = int(
        db.scalar(select(func.count(ImportItem.id)).where(ImportItem.created_at >= week_start)) or 0
    )
    accepted = int(
        db.scalar(
            select(func.count(ImportItem.id)).where(
                ImportItem.created_at >= week_start,
                ImportItem.status == ProcessingStatus.ACCEPTED,
            )
        )
        or 0
    )
    unresolved = int(
        db.scalar(
            select(func.count(ImportItem.id)).where(
                ImportItem.created_at >= week_start,
                ImportItem.status == ProcessingStatus.UNRESOLVED,
            )
        )
        or 0
    )
    compatibility_reviews = int(
        db.scalar(
            select(func.count(ReviewItem.id))
            .join(Document, Document.id == ReviewItem.document_id)
            .where(
                ReviewItem.status == ReviewStatus.OPEN,
                Document.processing_status != ProcessingStatus.UNRESOLVED,
            )
        )
        or 0
    )
    new_patients = int(
        db.scalar(select(func.count(Patient.id)).where(Patient.first_encounter_at >= week_start))
        or 0
    )
    returning_patients = int(
        db.scalar(
            select(func.count(func.distinct(Encounter.patient_id)))
            .join(Patient, Patient.id == Encounter.patient_id)
            .where(
                Encounter.encounter_at >= week_start,
                Patient.first_encounter_at < week_start,
            )
        )
        or 0
    )

    recent_patients = db.scalars(
        select(Patient)
        .options(selectinload(Patient.identities))
        .order_by(Patient.last_encounter_at.desc())
        .limit(5)
    ).all()
    recent_encounters = db.scalars(
        select(Encounter).order_by(Encounter.encounter_at.desc()).limit(5)
    ).all()
    recent_analyses = db.scalars(
        select(AIAnalysis).order_by(AIAnalysis.created_at.desc()).limit(5)
    ).all()
    recent_notes = db.scalars(
        select(StudentNote).order_by(StudentNote.created_at.desc()).limit(5)
    ).all()

    return {
        "timezone": settings.app_timezone,
        "week_start": week_start_local.date().isoformat(),
        "ocr_provider": settings.ocr_provider,
        "counts": {
            "imported": imported,
            "accepted": accepted,
            "unresolved": unresolved,
            "compatibility_reviews": compatibility_reviews,
            "new_patients": new_patients,
            "returning_patients": returning_patients,
        },
        "recent_patients": [
            {
                "id": str(patient.id),
                "code": patient.internal_patient_code,
                "sex": patient.sex.value,
                "last_encounter_at": patient.last_encounter_at,
            }
            for patient in recent_patients
        ],
        "recent_encounters": [
            {
                "id": str(encounter.id),
                "patient_id": str(encounter.patient_id),
                "encounter_at": encounter.encounter_at,
                "chief_complaint": encounter.chief_complaint_raw,
            }
            for encounter in recent_encounters
        ],
        "recent_analyses": [
            {
                "id": str(analysis.id),
                "task_type": analysis.task_type.value,
                "created_at": analysis.created_at,
            }
            for analysis in recent_analyses
        ],
        "recent_notes": [
            {"id": str(note.id), "note_kind": note.note_kind.value, "created_at": note.created_at}
            for note in recent_notes
        ],
    }
