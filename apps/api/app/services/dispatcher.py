from __future__ import annotations

import uuid

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.pipeline import PipelineResult, PipelineService


def dispatch_document(document_id: uuid.UUID) -> PipelineResult | None:
    if get_settings().tasks_eager:
        with SessionLocal() as session:
            return PipelineService(session).process(document_id)

    from app.workers.tasks import process_document_task

    process_document_task.delay(str(document_id))
    return None
