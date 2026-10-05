from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import ImportBatch, ImportItem
from app.models.enums import ProcessingStatus

TERMINAL = {
    ProcessingStatus.ACCEPTED,
    ProcessingStatus.REVIEW_REQUIRED,
    ProcessingStatus.UNRESOLVED,
    ProcessingStatus.FAILED,
}


def recalculate_batch(session: Session, batch_id: uuid.UUID) -> ImportBatch | None:
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        return None
    session.flush()
    statuses = list(
        session.scalars(select(ImportItem.status).where(ImportItem.batch_id == batch_id))
    )
    batch.total_items = len(statuses)
    batch.accepted_items = statuses.count(ProcessingStatus.ACCEPTED)
    batch.review_items = statuses.count(ProcessingStatus.REVIEW_REQUIRED)
    batch.failed_items = statuses.count(ProcessingStatus.FAILED)
    batch.unresolved_items = statuses.count(ProcessingStatus.UNRESOLVED)
    active = [status for status in statuses if status not in TERMINAL]
    if active:
        batch.status = active[0]
    elif batch.review_items:
        batch.status = ProcessingStatus.REVIEW_REQUIRED
    elif batch.unresolved_items:
        batch.status = ProcessingStatus.UNRESOLVED
    elif batch.failed_items:
        batch.status = ProcessingStatus.FAILED
    else:
        batch.status = ProcessingStatus.ACCEPTED
    return batch
