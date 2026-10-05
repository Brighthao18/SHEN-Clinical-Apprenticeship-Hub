from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.security import CurrentUser
from app.db.session import get_db
from app.models.domain import Document, Encounter, ImportBatch, ImportItem
from app.models.enums import DocumentType, ProcessingStatus
from app.schemas.api import ImportBatchRead, ImportBatchSummaryRead, ImportItemRead
from app.services.audit import append_audit_event
from app.services.dispatcher import dispatch_document
from app.services.imports import recalculate_batch
from app.services.storage import StorageError, get_storage

router = APIRouter(prefix="/imports", tags=["imports"])
DbSession = Annotated[Session, Depends(get_db)]

logger = logging.getLogger(__name__)


def _serialize_batch(session: Session, batch: ImportBatch) -> ImportBatchRead:
    items: list[ImportItemRead] = []
    for item in sorted(batch.items, key=lambda current: current.ordinal):
        encounter = None
        if item.document_id:
            encounter = session.scalar(
                select(Encounter).where(Encounter.primary_document_id == item.document_id)
            )
        items.append(
            ImportItemRead(
                id=item.id,
                ordinal=item.ordinal,
                original_filename=item.original_filename,
                document_id=item.document_id,
                status=item.status,
                error_code=item.error_code,
                patient_id=encounter.patient_id if encounter else None,
                encounter_id=encounter.id if encounter else None,
            )
        )
    return ImportBatchRead(
        id=batch.id,
        label=batch.label,
        status=batch.status,
        total_items=batch.total_items,
        accepted_items=batch.accepted_items,
        review_items=batch.review_items,
        failed_items=batch.failed_items,
        unresolved_items=batch.unresolved_items,
        created_at=batch.created_at,
        items=items,
    )


@router.post("", response_model=ImportBatchRead, status_code=status.HTTP_202_ACCEPTED)
async def create_import(
    user: CurrentUser,
    db: DbSession,
    files: Annotated[list[UploadFile], File(...)],
    document_type: Annotated[str, Form()] = DocumentType.OUTPATIENT_RECORD.value,
    label: Annotated[str | None, Form()] = None,
    mock_ocr_text: Annotated[str | None, Form()] = None,
) -> ImportBatchRead:
    settings = get_settings()
    if not files:
        raise HTTPException(status_code=422, detail="At least one file is required")
    if settings.ocr_provider == "mock" and (len(files) != 1 or not mock_ocr_text):
        raise HTTPException(
            status_code=422,
            detail="Mock OCR requires exactly one file and explicit mock_ocr_text",
        )
    try:
        parsed_document_type = DocumentType(document_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Invalid document_type") from exc

    batch = ImportBatch(label=label, total_items=len(files), status=ProcessingStatus.UPLOADED)
    db.add(batch)
    db.flush()
    dispatch_ids: list[uuid.UUID] = []
    storage = get_storage()

    for ordinal, upload in enumerate(files, start=1):
        data = await upload.read(settings.max_upload_bytes + 1)
        try:
            stored = storage.put_bytes(data, upload.filename or "upload", upload.content_type)
        except StorageError as exc:
            db.rollback()
            http_status = 413 if "size" in str(exc).lower() else 415
            raise HTTPException(status_code=http_status, detail=str(exc)) from exc

        document = db.scalar(select(Document).where(Document.sha256 == stored.sha256))
        if document is None:
            document = Document(
                original_filename=upload.filename or "upload",
                original_file_path=stored.key,
                sha256=stored.sha256,
                byte_size=stored.byte_size,
                mime_type=stored.mime_type,
                document_type=parsed_document_type,
                processing_status=ProcessingStatus.QUEUED,
                processing_payload={"mock_ocr_text": mock_ocr_text}
                if settings.ocr_provider == "mock"
                else {},
            )
            db.add(document)
            db.flush()
            dispatch_ids.append(document.id)
            append_audit_event(
                db,
                action="document.uploaded",
                entity_type="document",
                entity_id=document.id,
                actor_type="user",
                after_redacted={"sha256": stored.sha256, "byte_size": stored.byte_size},
            )

        item = ImportItem(
            batch_id=batch.id,
            ordinal=ordinal,
            original_filename=upload.filename or "upload",
            document_id=document.id,
            status=document.processing_status,
            error_code=document.failure_code,
        )
        db.add(item)

    recalculate_batch(db, batch.id)
    db.commit()

    # Dispatch failures must never strand committed rows in QUEUED: no worker
    # would ever pick them up and the retry endpoint does not accept QUEUED.
    for document_id in dispatch_ids:
        try:
            dispatch_document(document_id)
            continue
        except Exception:
            logger.warning(
                "Document dispatch failed after upload commit",
                extra={"document_id": str(document_id), "error_code": "DISPATCH_FAILED"},
            )
        # The pipeline/dispatcher runs on its own session; re-read the honest
        # post-attempt state before deciding what to record.
        db.expire_all()
        document = db.get(Document, document_id)
        if document is None:
            continue
        if (
            document.processing_status in {ProcessingStatus.UPLOADED, ProcessingStatus.QUEUED}
            and document.failure_code is None
        ):
            # Broker unreachable (or eager dispatch died before the pipeline
            # recorded a terminal): FAILED + DISPATCH_FAILED is the honest
            # retryable terminal, accepted by POST /documents/{id}/retry.
            document.processing_status = ProcessingStatus.FAILED
            document.failure_code = "DISPATCH_FAILED"
        for item in batch.items:
            if item.document_id == document.id:
                item.status = document.processing_status
                item.error_code = document.failure_code
        # Persist before the next iteration's expire_all discards unflushed
        # markings from an earlier failure in the same batch.
        db.flush()

    db.expire_all()
    refreshed = db.get(ImportBatch, batch.id)
    assert refreshed is not None
    recalculate_batch(db, refreshed.id)
    db.commit()
    db.refresh(refreshed)
    return _serialize_batch(db, refreshed)


@router.get("", response_model=list[ImportBatchSummaryRead])
def list_imports(
    user: CurrentUser,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> list[ImportBatchSummaryRead]:
    """Recent batches so a refresh or re-entry can resume where the upload
    left off (the workspace keeps only the current batch in memory)."""
    batches = db.scalars(
        select(ImportBatch).order_by(ImportBatch.created_at.desc()).limit(limit)
    ).all()
    return [
        ImportBatchSummaryRead(
            id=batch.id,
            label=batch.label,
            status=batch.status,
            total_items=batch.total_items,
            accepted_items=batch.accepted_items,
            review_items=batch.review_items,
            failed_items=batch.failed_items,
            unresolved_items=batch.unresolved_items,
            created_at=batch.created_at,
        )
        for batch in batches
    ]


@router.get("/{batch_id}", response_model=ImportBatchRead)
def get_import(batch_id: uuid.UUID, user: CurrentUser, db: DbSession) -> ImportBatchRead:
    batch = db.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Import batch not found")
    recalculate_batch(db, batch.id)
    db.commit()
    return _serialize_batch(db, batch)
