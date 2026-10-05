"""Celery task layer tests (called inline, never through the broker).

Covers the eager inline dispatch contract: the task returns the terminal
PipelineResult summary, and a retryable pipeline failure is re-raised as a
Celery Retry with bounded retries instead of being swallowed.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.domain import Document, DocumentType, UnresolvedIngestion
from app.models.enums import ProcessingStatus
from app.services.pipeline import PipelineRetryableError
from app.workers.tasks import process_document_task

STORAGE_ROOT = Path(__file__).parents[3] / "work" / "test-storage"
UNKNOWN_TEMPLATE_OCR = "完全没有任何模板特征的文本 12345"


def _document(label: str, ocr_text: str) -> Document:
    digest = hashlib.sha256(label.encode()).hexdigest()
    document = Document(
        id=uuid.uuid4(),
        original_filename=f"{label}.jpg",
        original_file_path=f"synthetic/{digest}.jpg",
        sha256=digest,
        byte_size=10,
        mime_type="image/jpeg",
        document_type=DocumentType.OUTPATIENT_RECORD,
        processing_status=ProcessingStatus.UPLOADED,
        ocr_text=ocr_text,
    )
    path = STORAGE_ROOT / document.original_file_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic-image-bytes")
    return document


def test_task_reports_unresolved_terminal_without_clinical_rows() -> None:
    document = _document("task-unresolved", UNKNOWN_TEMPLATE_OCR)
    with SessionLocal() as session:
        session.add(document)
        session.commit()

        result = process_document_task(str(document.id))

        assert result["document_id"] == str(document.id)
        assert result["status"] == "UNRESOLVED"
        assert result["encounter_id"] is None
        # A blocking UNRESOLVED reason also opens one ReviewItem row; the
        # compatibility queue hides UNRESOLVED documents, so only direct DB
        # inspection sees it.
        assert result["review_count"] == 1
        ledger = session.scalar(select(UnresolvedIngestion))
        assert ledger is not None
        assert ledger.document_id == document.id


def test_task_converts_retryable_failure_into_bounded_retry() -> None:
    document = _document("task-retry", "任意文本")
    with SessionLocal() as session:
        session.add(document)
        session.commit()

        class _FailingPipeline:
            def __init__(self, session) -> None:
                self.session = session

            def process(self, document_id: uuid.UUID):
                raise PipelineRetryableError("OCR_PROVIDER_TIMEOUT")

        from unittest.mock import patch

        # Bounded retry contract: a retryable failure is re-raised through
        # self.retry; under eager/direct invocation Celery re-raises the
        # original exception instead of scheduling.
        assert process_document_task.max_retries == 3
        with patch("app.workers.tasks.PipelineService", _FailingPipeline):
            eager = process_document_task.apply(args=[str(document.id)])
        assert eager.failed
        assert isinstance(eager.result, PipelineRetryableError)
