from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import NoteKind, ProcessingStatus, ReviewStatus


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class ImportItemRead(BaseModel):
    id: uuid.UUID
    ordinal: int
    original_filename: str
    document_id: uuid.UUID | None
    status: ProcessingStatus
    error_code: str | None = None
    patient_id: uuid.UUID | None = None
    encounter_id: uuid.UUID | None = None


class ImportBatchRead(BaseModel):
    id: uuid.UUID
    label: str | None
    status: ProcessingStatus
    total_items: int
    accepted_items: int
    review_items: int
    failed_items: int
    unresolved_items: int = 0
    created_at: datetime
    items: list[ImportItemRead]


class ImportBatchSummaryRead(BaseModel):
    """Lightweight batch row for the recent-batches picker (no items)."""

    id: uuid.UUID
    label: str | None
    status: ProcessingStatus
    total_items: int
    accepted_items: int
    review_items: int
    failed_items: int
    unresolved_items: int = 0
    created_at: datetime


class ReviewItemRead(BaseModel):
    id: uuid.UUID
    document_id: uuid.UUID
    encounter_id: uuid.UUID | None
    prescription_item_id: uuid.UUID | None
    issue_code: str
    field_path: str
    severity: str
    reason_category: str | None = None
    criticality: str | None = None
    blocking: bool = True
    observed_value: dict[str, Any]
    proposed_value: dict[str, Any] | None
    status: ReviewStatus
    resolution_note: str | None
    created_at: datetime
    row_version: int
    # Encounter context (joined for the compatibility review UI; lets the
    # specification-supplement form prefill hospital and effective date).
    hospital: str | None = None
    encounter_date: date | None = None

    model_config = ConfigDict(from_attributes=True)


class ReviewResolveRequest(BaseModel):
    note: str = Field(min_length=1, max_length=1000)
    value: dict[str, Any] | None = None
    expected_row_version: int


class UnresolvedIngestionRead(BaseModel):
    """Unresolved-ingestion queue row; contains stable IDs only, no PII."""

    id: uuid.UUID
    document_id: uuid.UUID
    template: str
    failure_stage: ProcessingStatus
    failure_reason_code: str
    reason_category: str
    critical_fields_affected: list[str]
    parser_name: str | None
    parser_version: str | None
    normalizer_version: str | None
    validation_policy_version: str
    ocr_provider: str | None
    ocr_version: str | None
    retry_count: int
    last_attempt_at: datetime
    resolved_at: datetime | None
    resolution: str | None

    model_config = ConfigDict(from_attributes=True)


class StudentNoteCreate(BaseModel):
    note_kind: NoteKind
    content: str = Field(min_length=1, max_length=20_000)
    request_id: uuid.UUID | None = None


class StudentNoteRead(BaseModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    encounter_id: uuid.UUID | None
    note_kind: NoteKind
    content: str
    version: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PrescriptionDiffEncounterRead(BaseModel):
    id: uuid.UUID
    encounter_at: datetime
    document_id: uuid.UUID


class PrescriptionDiffEvidenceRead(BaseModel):
    previous_encounter_id: uuid.UUID | None
    current_encounter_id: uuid.UUID | None
    previous_document_id: uuid.UUID | None
    current_document_id: uuid.UUID | None
    previous_prescription_item_id: uuid.UUID | None
    current_prescription_item_id: uuid.UUID | None


class PrescriptionDiffSummaryRead(BaseModel):
    total: int
    added: int
    removed: int
    dose_increased: int
    dose_decreased: int
    processing_changed: int
    unchanged: int
    normalization_pending: int
    unknown_dose: int


class PrescriptionDiffItemRead(BaseModel):
    primary_change: str
    change_types: list[str]
    identity_basis: str
    canonical_identity: str | None
    previous_raw_name: str | None
    current_raw_name: str | None
    previous_processing: str | None
    current_processing: str | None
    processing_changed: bool
    previous_dose_g: str | None
    current_dose_g: str | None
    signed_change_g: str | None
    absolute_change_g: str | None
    dose_change: str
    previous_dose_scope: str | None
    current_dose_scope: str | None
    previous_normalization_status: str | None
    current_normalization_status: str | None
    evidence: PrescriptionDiffEvidenceRead


class PrescriptionDiffRead(BaseModel):
    algorithm_version: str
    dose_basis: str
    previous_encounter: PrescriptionDiffEncounterRead
    current_encounter: PrescriptionDiffEncounterRead
    summary: PrescriptionDiffSummaryRead
    items: list[PrescriptionDiffItemRead]
