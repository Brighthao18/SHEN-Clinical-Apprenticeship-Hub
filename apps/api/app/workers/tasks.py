from __future__ import annotations

import uuid

from app.db.session import SessionLocal
from app.services.pipeline import PipelineRetryableError, PipelineService
from app.workers.celery_app import celery_app


@celery_app.task(bind=True, max_retries=3, name="shen.process_document")
def process_document_task(self, document_id: str) -> dict[str, str | int | None]:
    try:
        with SessionLocal() as session:
            result = PipelineService(session).process(uuid.UUID(document_id))
        return {
            "document_id": str(result.document_id),
            "status": result.status.value,
            "encounter_id": str(result.encounter_id) if result.encounter_id else None,
            "review_count": result.review_count,
        }
    except PipelineRetryableError as exc:
        countdown = min(60, 2 ** (self.request.retries + 1))
        raise self.retry(exc=exc, countdown=countdown) from exc
