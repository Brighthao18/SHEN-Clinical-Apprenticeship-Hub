from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.security import CurrentUser
from app.db.base import utcnow
from app.db.session import get_db
from app.models.domain import (
    Document,
    Encounter,
    ImportItem,
    PrescriptionItem,
    ReviewItem,
)
from app.models.enums import ProcessingStatus, ReviewStatus
from app.schemas.api import ReviewItemRead, ReviewResolveRequest
from app.services.audit import append_audit_event
from app.services.imports import recalculate_batch

router = APIRouter(prefix="/reviews", tags=["reviews"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("", response_model=list[ReviewItemRead])
def list_reviews(
    user: CurrentUser,
    db: DbSession,
    review_status: Annotated[ReviewStatus, Query(alias="status")] = ReviewStatus.OPEN,
) -> list[ReviewItemRead]:
    rows = db.execute(
        select(ReviewItem, Encounter)
        .join(Document, Document.id == ReviewItem.document_id)
        .join(Encounter, Encounter.id == ReviewItem.encounter_id, isouter=True)
        .where(
            ReviewItem.status == review_status,
            Document.processing_status != ProcessingStatus.UNRESOLVED,
        )
        .order_by(ReviewItem.created_at.desc())
    ).all()
    return [
        ReviewItemRead.model_validate(review).model_copy(
            update={
                "hospital": encounter.hospital if encounter is not None else None,
                "encounter_date": (
                    encounter.encounter_at.date() if encounter is not None else None
                ),
            }
        )
        for review, encounter in rows
    ]


@router.post("/{review_id}/resolve", response_model=ReviewItemRead)
def resolve_review(
    review_id: uuid.UUID,
    payload: ReviewResolveRequest,
    user: CurrentUser,
    db: DbSession,
) -> ReviewItem:
    review = db.get(ReviewItem, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="Review item not found")
    if review.status != ReviewStatus.OPEN:
        raise HTTPException(status_code=409, detail="Review item is already closed")
    if review.row_version != payload.expected_row_version:
        raise HTTPException(status_code=409, detail="Review item changed; refresh and try again")

    review.status = ReviewStatus.RESOLVED
    review.resolution_note = payload.note
    review.proposed_value = payload.value
    review.resolved_at = utcnow()
    review.row_version += 1
    db.flush()

    if review.prescription_item_id:
        remaining_for_item = int(
            db.scalar(
                select(func.count(ReviewItem.id)).where(
                    ReviewItem.prescription_item_id == review.prescription_item_id,
                    ReviewItem.status == ReviewStatus.OPEN,
                )
            )
            or 0
        )
        if remaining_for_item == 0:
            prescription_item = db.get(PrescriptionItem, review.prescription_item_id)
            if prescription_item:
                prescription_item.review_status = ReviewStatus.RESOLVED

    remaining = int(
        db.scalar(
            select(func.count(ReviewItem.id)).where(
                ReviewItem.document_id == review.document_id,
                ReviewItem.status == ReviewStatus.OPEN,
            )
        )
        or 0
    )
    encounter = db.scalar(
        select(Encounter).where(Encounter.primary_document_id == review.document_id)
    )
    document = db.get(Document, review.document_id)
    if remaining == 0 and encounter is not None and document is not None:
        document.processing_status = ProcessingStatus.ACCEPTED
        import_items = db.scalars(
            select(ImportItem).where(ImportItem.document_id == document.id)
        ).all()
        for import_item in import_items:
            import_item.status = ProcessingStatus.ACCEPTED
        for batch_id in {import_item.batch_id for import_item in import_items}:
            recalculate_batch(db, batch_id)

    append_audit_event(
        db,
        action="review.resolved",
        entity_type="review_item",
        entity_id=review.id,
        patient_id=encounter.patient_id if encounter else None,
        actor_type="user",
        after_redacted={"status": "resolved", "value_supplied": payload.value is not None},
    )
    db.commit()
    db.refresh(review)
    return review
