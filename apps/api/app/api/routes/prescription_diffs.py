from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import CurrentUser
from app.db.session import get_db
from app.models.domain import Encounter, Prescription
from app.schemas.api import PrescriptionDiffRead
from app.services.prescription_diff import (
    AmbiguousPrescriptionDiffError,
    compare_prescriptions,
)

router = APIRouter(tags=["prescription-diffs"])
DbSession = Annotated[Session, Depends(get_db)]


def _load_encounter(db: Session, encounter_id: uuid.UUID) -> Encounter | None:
    return db.scalar(
        select(Encounter)
        .where(Encounter.id == encounter_id)
        .options(selectinload(Encounter.prescriptions).selectinload(Prescription.items))
    )


def _order_pair(first: Encounter, second: Encounter) -> tuple[Encounter, Encounter]:
    ordered = sorted((first, second), key=lambda value: (value.encounter_at, str(value.id)))
    return ordered[0], ordered[1]


def _previous_encounter(db: Session, current: Encounter) -> Encounter | None:
    siblings = list(
        db.scalars(select(Encounter).where(Encounter.patient_id == current.patient_id)).all()
    )
    siblings.sort(key=lambda value: (value.encounter_at, str(value.id)))
    current_index = next(
        (index for index, encounter in enumerate(siblings) if encounter.id == current.id), None
    )
    if current_index is None or current_index == 0:
        return None
    return _load_encounter(db, siblings[current_index - 1].id)


@router.get(
    "/encounters/{encounter_id}/prescription-diff",
    response_model=PrescriptionDiffRead,
)
def get_prescription_diff(
    encounter_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    compare_to_encounter_id: Annotated[uuid.UUID | None, Query()] = None,
) -> dict[str, Any]:
    selected = _load_encounter(db, encounter_id)
    if selected is None:
        raise HTTPException(status_code=404, detail="Encounter not found")

    if compare_to_encounter_id is None:
        previous = _previous_encounter(db, selected)
        if previous is None:
            raise HTTPException(status_code=404, detail="No previous encounter")
        current = selected
    else:
        if compare_to_encounter_id == selected.id:
            raise HTTPException(status_code=422, detail="请选择两次不同的就诊")
        compared = _load_encounter(db, compare_to_encounter_id)
        if compared is None:
            raise HTTPException(status_code=404, detail="Comparison encounter not found")
        if compared.patient_id != selected.patient_id:
            raise HTTPException(status_code=422, detail="只能比较同一患者的就诊")
        previous, current = _order_pair(selected, compared)

    try:
        return compare_prescriptions(previous, current)
    except AmbiguousPrescriptionDiffError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "PRESCRIPTION_DIFF_AMBIGUOUS："
                "处方中存在无法唯一配对的重复药物，系统已停止比较以避免猜测"
            ),
        ) from exc
