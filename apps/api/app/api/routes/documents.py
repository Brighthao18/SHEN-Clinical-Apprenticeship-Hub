from __future__ import annotations

import logging
import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import CurrentUser
from app.db.session import get_db
from app.models.domain import Document, ProcessingJob
from app.models.enums import JobStatus, ProcessingStatus
from app.services.dispatcher import dispatch_document
from app.services.storage import get_storage

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents", tags=["documents"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/{document_id}/content", response_class=FileResponse)
def document_content(document_id: uuid.UUID, user: CurrentUser, db: DbSession) -> FileResponse:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    try:
        path = get_storage().path_for(document.original_file_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Stored document content not found") from exc
    safe_name = quote(document.original_filename)
    return FileResponse(
        path,
        media_type=document.mime_type,
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{safe_name}"},
    )


@router.post("/{document_id}/retry")
def retry_document(document_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, str]:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found")
    # QUEUED is accepted so a dispatch that died before any worker claimed
    # the job can be re-enqueued without SQL surgery; the pipeline's job
    # claim keeps a re-dispatch idempotent.
    if document.processing_status.value not in {"FAILED", "REVIEW_REQUIRED", "QUEUED"}:
        raise HTTPException(status_code=409, detail="Document is not in a retryable state")
    document.processing_status = ProcessingStatus.QUEUED
    document.failure_code = None
    job = db.scalar(select(ProcessingJob).where(ProcessingJob.document_id == document.id))
    if job is not None:
        job.status = JobStatus.RETRY_SCHEDULED
        job.stage = ProcessingStatus.QUEUED
    db.commit()
    try:
        dispatch_document(document.id)
    except Exception:
        # Broker unreachable: QUEUED would be a lie and no worker would ever
        # pick this up. Record the honest retryable terminal instead.
        logger.warning(
            "Document dispatch failed; broker unreachable",
            extra={"document_id": str(document_id), "error_code": "DISPATCH_FAILED"},
        )
        document.processing_status = ProcessingStatus.FAILED
        document.failure_code = "DISPATCH_FAILED"
        if job is not None:
            job.status = JobStatus.FAILED
            job.stage = ProcessingStatus.FAILED
            job.error_code = "DISPATCH_FAILED"
        db.commit()
        raise HTTPException(
            status_code=503,
            detail="任务队列暂时不可用,文档已标记为失败,请稍后重试",
        ) from None
    return {"document_id": str(document.id), "status": "QUEUED"}
