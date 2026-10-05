from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, VersionedMixin, utcnow
from app.db.types import EncryptedText
from app.models.enums import (
    AITaskType,
    ApprenticeshipNoteType,
    DiagnosisType,
    DocumentType,
    DoseScope,
    DoseSource,
    JobStatus,
    NoteKind,
    NoteVersionType,
    ProcessingStatus,
    ReviewStatus,
    Sex,
    SourceType,
    SymptomStatus,
    SymptomTrend,
)


def enum_type(enum_class: type, name: str) -> SqlEnum:
    return SqlEnum(
        enum_class,
        values_callable=lambda values: [item.value for item in values],
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        name=name,
    )


class SystemCounter(Base):
    __tablename__ = "system_counters"

    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class Patient(UUIDPrimaryKeyMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "patients"
    __table_args__ = (
        Index("ix_patients_last_encounter", "last_encounter_at"),
        Index("ix_patients_demographics", "sex", "estimated_birth_year"),
    )

    internal_patient_code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    sex: Mapped[Sex] = mapped_column(
        enum_type(Sex, "sex_enum"), default=Sex.UNKNOWN, nullable=False
    )
    birth_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    estimated_birth_year: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    first_encounter_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_encounter_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    identities: Mapped[list[PatientIdentity]] = relationship(
        back_populates="patient", cascade="all, delete-orphan"
    )
    encounters: Mapped[list[Encounter]] = relationship(back_populates="patient")


class PatientIdentity(UUIDPrimaryKeyMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "patient_identities"
    __table_args__ = (
        UniqueConstraint("hospital", "mrn_fingerprint", name="uq_identity_hospital_mrn"),
        Index("ix_identity_patient", "patient_id"),
        Index("ix_identity_name_fingerprint", "real_name_fingerprint"),
    )

    patient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    hospital: Mapped[str] = mapped_column(String(160), nullable=False)
    real_name: Mapped[str | None] = mapped_column(EncryptedText(), nullable=True)
    phone: Mapped[str | None] = mapped_column(EncryptedText(), nullable=True)
    id_card: Mapped[str | None] = mapped_column(EncryptedText(), nullable=True)
    address: Mapped[str | None] = mapped_column(EncryptedText(), nullable=True)
    hospital_medical_record_number: Mapped[str | None] = mapped_column(
        EncryptedText(), nullable=True
    )
    real_name_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mrn_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )

    patient: Mapped[Patient] = relationship(back_populates="identities")


class ImportBatch(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "import_batches"
    __table_args__ = (Index("ix_import_batches_created_at", "created_at"),)

    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[ProcessingStatus] = mapped_column(
        enum_type(ProcessingStatus, "processing_status_enum"),
        default=ProcessingStatus.UPLOADED,
        nullable=False,
    )
    total_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    accepted_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    review_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unresolved_items: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    items: Mapped[list[ImportItem]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="ImportItem.ordinal"
    )


class ImportItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "import_items"
    __table_args__ = (
        UniqueConstraint("batch_id", "ordinal", name="uq_import_item_ordinal"),
        Index("ix_import_items_status", "status"),
        Index("ix_import_items_document", "document_id"),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[ProcessingStatus] = mapped_column(
        enum_type(ProcessingStatus, "import_item_status_enum"),
        default=ProcessingStatus.UPLOADED,
        nullable=False,
    )
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)

    batch: Mapped[ImportBatch] = relationship(back_populates="items")
    document: Mapped[Document | None] = relationship(back_populates="import_items")


class Document(UUIDPrimaryKeyMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_processing_status", "processing_status"),
        Index("ix_documents_uploaded_at", "uploaded_at"),
    )

    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    original_file_path: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    document_type: Mapped[DocumentType] = mapped_column(
        enum_type(DocumentType, "document_type_enum"), nullable=False
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    ocr_provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ocr_raw_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    processing_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    structured_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    parser_template: Mapped[str | None] = mapped_column(String(100), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        enum_type(ProcessingStatus, "document_processing_status_enum"),
        default=ProcessingStatus.UPLOADED,
        nullable=False,
    )
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)

    import_items: Mapped[list[ImportItem]] = relationship(back_populates="document")
    jobs: Mapped[list[ProcessingJob]] = relationship(back_populates="document")
    provider_submissions: Mapped[list[OCRProviderSubmission]] = relationship(
        back_populates="document"
    )


class ProcessingJob(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "processing_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_processing_job_idempotency"),
        Index("ix_processing_jobs_document_stage", "document_id", "stage", "created_at"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    pipeline_version: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[ProcessingStatus] = mapped_column(
        enum_type(ProcessingStatus, "processing_job_stage_enum"), nullable=False
    )
    status: Mapped[JobStatus] = mapped_column(
        enum_type(JobStatus, "job_status_enum"), default=JobStatus.PENDING, nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    provider_task_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_detail_redacted: Mapped[str | None] = mapped_column(String(500), nullable=True)

    document: Mapped[Document] = relationship(back_populates="jobs")
    provider_submissions: Mapped[list[OCRProviderSubmission]] = relationship(
        back_populates="processing_job"
    )


class OCRProviderSubmission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "ocr_provider_submissions"
    __table_args__ = (
        UniqueConstraint("processing_job_id", "attempt", name="uq_ocr_provider_submission_attempt"),
        Index("ix_ocr_provider_submission_document", "document_id", "submitted_at"),
        CheckConstraint("attempt > 0", name="ck_ocr_provider_submission_attempt_positive"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    processing_job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("processing_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_request_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(80), nullable=True)

    document: Mapped[Document] = relationship(back_populates="provider_submissions")
    processing_job: Mapped[ProcessingJob] = relationship(back_populates="provider_submissions")


class Encounter(UUIDPrimaryKeyMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "encounters"
    __table_args__ = (
        Index("ix_encounters_patient_date", "patient_id", "encounter_at"),
        Index("ix_encounters_hospital_date", "hospital", "encounter_at"),
    )

    patient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    primary_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    hospital: Mapped[str] = mapped_column(String(160), nullable=False)
    department: Mapped[str | None] = mapped_column(String(160), nullable=True)
    physician: Mapped[str | None] = mapped_column(String(120), nullable=True)
    encounter_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    age_at_encounter: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    chief_complaint_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    present_illness_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    past_history_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    allergy_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    vital_signs_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    advice_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    treatment_record_raw: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_source_type: Mapped[SourceType] = mapped_column(
        enum_type(SourceType, "encounter_source_type_enum"),
        default=SourceType.PHYSICIAN_RECORD,
        nullable=False,
    )

    patient: Mapped[Patient] = relationship(back_populates="encounters")
    primary_document: Mapped[Document] = relationship(foreign_keys=[primary_document_id])
    symptoms: Mapped[list[SymptomObservation]] = relationship(
        back_populates="encounter", cascade="all, delete-orphan"
    )
    diagnoses: Mapped[list[Diagnosis]] = relationship(
        back_populates="encounter", cascade="all, delete-orphan"
    )
    prescriptions: Mapped[list[Prescription]] = relationship(
        back_populates="encounter", cascade="all, delete-orphan"
    )
    student_notes: Mapped[list[StudentNote]] = relationship(back_populates="encounter")
    analyses: Mapped[list[AIAnalysis]] = relationship(back_populates="encounter")


class EncounterDocument(Base):
    __tablename__ = "encounter_documents"
    __table_args__ = (UniqueConstraint("document_id", name="uq_encounter_document"),)

    encounter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(40), default="primary", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SymptomObservation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "symptom_observations"
    __table_args__ = (
        Index("ix_symptoms_encounter", "encounter_id"),
        Index("ix_symptoms_normalized", "symptom_normalized", "status"),
    )

    encounter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False
    )
    symptom_raw: Mapped[str] = mapped_column(String(255), nullable=False)
    symptom_normalized: Mapped[str | None] = mapped_column(String(160), nullable=True)
    status: Mapped[SymptomStatus] = mapped_column(
        enum_type(SymptomStatus, "symptom_status_enum"), nullable=False
    )
    trend: Mapped[SymptomTrend | None] = mapped_column(
        enum_type(SymptomTrend, "symptom_trend_enum"), nullable=True
    )
    duration: Mapped[str | None] = mapped_column(String(120), nullable=True)
    severity: Mapped[str | None] = mapped_column(String(120), nullable=True)
    location: Mapped[str | None] = mapped_column(String(120), nullable=True)
    frequency: Mapped[str | None] = mapped_column(String(120), nullable=True)
    source_type: Mapped[SourceType] = mapped_column(
        enum_type(SourceType, "symptom_source_type_enum"), nullable=False
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    evidence_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    normalization_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    encounter: Mapped[Encounter] = relationship(back_populates="symptoms")


class Diagnosis(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "diagnoses"
    __table_args__ = (
        Index("ix_diagnoses_encounter", "encounter_id"),
        Index("ix_diagnoses_normalized", "diagnosis_type", "name_normalized"),
        Index("ix_diagnoses_source", "source_type"),
    )

    encounter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False
    )
    diagnosis_type: Mapped[DiagnosisType] = mapped_column(
        enum_type(DiagnosisType, "diagnosis_type_enum"), nullable=False
    )
    name_raw: Mapped[str] = mapped_column(String(255), nullable=False)
    name_normalized: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source_type: Mapped[SourceType] = mapped_column(
        enum_type(SourceType, "diagnosis_source_type_enum"), nullable=False
    )
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    evidence_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    normalization_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    encounter: Mapped[Encounter] = relationship(back_populates="diagnoses")


class DrugDictionary(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Master-data drug dictionary with a PENDING candidate lifecycle.

    Phase 6.2: a missing canonical mapping never blocks ingestion. Instead a
    PENDING candidate row is created/reused for the printed raw name and
    accumulates usage; user confirmation flips it to VERIFIED and backfills
    historical items. DISABLED rows are known-bad and never resolve.
    """

    __tablename__ = "drug_dictionary"
    __table_args__ = (
        UniqueConstraint(
            "canonical_name",
            "processing_method",
            "dictionary_version",
            name="uq_drug_dictionary_version",
        ),
        Index("ix_drug_dictionary_canonical", "canonical_name"),
        Index("ix_drug_dictionary_status", "status"),
        # Declared as a unique INDEX to match migration f2a4c6e8b930 exactly;
        # a column-level unique=True would render a UniqueConstraint and make
        # `alembic check` report representation drift.
        Index("ix_drug_dictionary_candidate_raw", "candidate_raw_name", unique=True),
    )

    canonical_name: Mapped[str] = mapped_column(String(160), nullable=False)
    processing_method: Mapped[str | None] = mapped_column(String(120), nullable=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    hospital_display_names: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    common_ocr_errors: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    dictionary_version: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    # PENDING (auto candidate) / VERIFIED (resolvable) / DISABLED (never resolves)
    status: Mapped[str] = mapped_column(
        String(20), default="VERIFIED", server_default="VERIFIED", nullable=False
    )
    # The printed raw drug name this candidate covers; unique across entries
    # (enforced by the unique index declared in __table_args__).
    candidate_raw_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    representative_evidence: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    source_document_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class HospitalDrugSpecification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "hospital_drug_specifications"
    __table_args__ = (
        UniqueConstraint(
            "hospital",
            "drug_dictionary_id",
            "processing_method",
            "normalized_package_unit",
            "effective_from",
            "version",
            name="uq_hospital_drug_spec_version",
        ),
        CheckConstraint("package_dose_g > 0", name="ck_spec_positive_dose"),
        CheckConstraint(
            "source_type IN ('manual_verified', 'legacy_unverified')",
            name="ck_spec_source_type",
        ),
        Index("ix_hospital_spec_lookup", "hospital", "drug_dictionary_id", "effective_from"),
    )

    hospital: Mapped[str] = mapped_column(String(160), nullable=False)
    drug_dictionary_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("drug_dictionary.id", ondelete="RESTRICT"), nullable=False
    )
    processing_method: Mapped[str | None] = mapped_column(String(120), nullable=True)
    package_dose_g: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    normalized_package_unit: Mapped[str] = mapped_column(String(40), nullable=False)
    effective_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_type: Mapped[str] = mapped_column(String(40), default="manual_verified", nullable=False)
    source_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)

    drug: Mapped[DrugDictionary] = relationship()


class Prescription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "prescriptions"
    __table_args__ = (CheckConstraint("total_decoctions > 0", name="ck_total_decoctions_positive"),)

    encounter_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False
    )
    evidence_document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    total_decoctions: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    raw_instruction_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_type: Mapped[SourceType] = mapped_column(
        enum_type(SourceType, "prescription_source_type_enum"),
        default=SourceType.PHYSICIAN_RECORD,
        nullable=False,
    )

    encounter: Mapped[Encounter] = relationship(back_populates="prescriptions")
    items: Mapped[list[PrescriptionItem]] = relationship(
        back_populates="prescription",
        cascade="all, delete-orphan",
        order_by="PrescriptionItem.line_number",
    )


class PrescriptionItem(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "prescription_items"
    __table_args__ = (
        UniqueConstraint("prescription_id", "line_number", name="uq_prescription_line"),
        # Matches migration d9f0b2c4e617; keeps `alembic check` drift-free.
        Index("ix_prescription_items_normalization", "normalization_status"),
        CheckConstraint(
            "explicit_package_dose_g IS NULL OR explicit_package_dose_g >= 0",
            name="ck_item_explicit_dose_nonnegative",
        ),
        CheckConstraint(
            "package_count IS NULL OR package_count > 0", name="ck_item_package_count_positive"
        ),
        CheckConstraint(
            "calculated_dose_g IS NULL OR calculated_dose_g >= 0",
            name="ck_item_calculated_dose_nonnegative",
        ),
        Index("ix_prescription_items_canonical", "canonical_drug_name"),
        Index("ix_prescription_items_review", "review_status"),
    )

    prescription_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("prescriptions.id", ondelete="CASCADE"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)
    original_text: Mapped[str] = mapped_column(Text, nullable=False)
    is_basic_drug: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_drug_name: Mapped[str] = mapped_column(String(160), nullable=False)
    drug_dictionary_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("drug_dictionary.id", ondelete="RESTRICT"), nullable=True
    )
    canonical_drug_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # Phase 6.1: AI proposal kept for dictionary growth; never authoritative.
    ai_suggested_canonical_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    # "normalized" or "pending": pending items have a traceable raw name but
    # no canonical mapping yet; analytics may exclude them until confirmed.
    normalization_status: Mapped[str] = mapped_column(
        String(20), default="normalized", server_default="normalized", nullable=False
    )
    processing_method: Mapped[str | None] = mapped_column(String(120), nullable=True)
    explicit_package_dose_g: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    package_count: Mapped[Decimal | None] = mapped_column(Numeric(10, 3), nullable=True)
    original_package_unit: Mapped[str | None] = mapped_column(String(20), nullable=True)
    normalized_package_unit: Mapped[str | None] = mapped_column(String(40), nullable=True)
    calculated_dose_g: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    dose_source: Mapped[DoseSource] = mapped_column(
        enum_type(DoseSource, "dose_source_enum"), default=DoseSource.UNKNOWN, nullable=False
    )
    dose_scope: Mapped[DoseScope] = mapped_column(
        enum_type(DoseScope, "dose_scope_enum"), default=DoseScope.UNKNOWN, nullable=False
    )
    hospital_specification_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("hospital_drug_specifications.id", ondelete="RESTRICT"),
        nullable=True,
    )
    parser_version: Mapped[str] = mapped_column(String(32), nullable=False)
    normalization_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    review_status: Mapped[ReviewStatus] = mapped_column(
        enum_type(ReviewStatus, "prescription_item_review_status_enum"),
        default=ReviewStatus.RESOLVED,
        nullable=False,
    )

    prescription: Mapped[Prescription] = relationship(back_populates="items")
    drug: Mapped[DrugDictionary | None] = relationship(foreign_keys=[drug_dictionary_id])
    hospital_specification: Mapped[HospitalDrugSpecification | None] = relationship(
        foreign_keys=[hospital_specification_id]
    )


class ReviewItem(UUIDPrimaryKeyMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "review_items"
    __table_args__ = (
        Index("ix_review_items_status_created", "status", "created_at"),
        Index("ix_review_items_document", "document_id"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("encounters.id", ondelete="RESTRICT"), nullable=True
    )
    prescription_item_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("prescription_items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    issue_code: Mapped[str] = mapped_column(String(80), nullable=False)
    field_path: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="warning", nullable=False)
    # Phase 5.2 policy classification (validation_policy.py); nullable for
    # pre-5.2 rows, which stay blocking-compatible by default.
    reason_category: Mapped[str | None] = mapped_column(String(40), nullable=True)
    criticality: Mapped[str | None] = mapped_column(String(20), nullable=True)
    blocking: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    observed_value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    proposed_value: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[ReviewStatus] = mapped_column(
        enum_type(ReviewStatus, "review_item_status_enum"),
        default=ReviewStatus.OPEN,
        nullable=False,
    )
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class UnresolvedIngestion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Phase 5.2 unresolved-ingestion queue.

    One row per document whose ingestion terminated UNRESOLVED. This is a
    retry ledger for parser upgrades, not a human work queue: unresolved
    documents create no committed clinical rows and expect no manual action.
    An explicit ``reprocess_unresolved`` command (dry-run first) may replay
    them after a parser/policy upgrade.
    """

    __tablename__ = "unresolved_ingestions"
    __table_args__ = (
        UniqueConstraint("document_id", name="uq_unresolved_ingestions_document"),
        Index("ix_unresolved_ingestions_template", "template"),
        Index("ix_unresolved_ingestions_last_attempt", "last_attempt_at"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    template: Mapped[str] = mapped_column(String(100), nullable=False)
    failure_stage: Mapped[str] = mapped_column(
        enum_type(ProcessingStatus, "processing_job_stage_enum"),
        default=ProcessingStatus.VALIDATING,
        nullable=False,
    )
    failure_reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    reason_category: Mapped[str] = mapped_column(String(40), nullable=False)
    critical_fields_affected: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    parser_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    normalizer_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    validation_policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    ocr_provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ocr_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(40), nullable=True)

    document: Mapped[Document] = relationship()


class StudentNote(UUIDPrimaryKeyMixin, TimestampMixin, VersionedMixin, Base):
    __tablename__ = "student_notes"
    __table_args__ = (Index("ix_student_notes_patient_encounter", "patient_id", "encounter_id"),)

    patient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("encounters.id", ondelete="RESTRICT"), nullable=True
    )
    note_kind: Mapped[NoteKind] = mapped_column(
        enum_type(NoteKind, "note_kind_enum"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[SourceType] = mapped_column(
        enum_type(SourceType, "student_note_source_type_enum"),
        default=SourceType.STUDENT_NOTE,
        nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("student_notes.id", ondelete="RESTRICT"), nullable=True
    )

    patient: Mapped[Patient] = relationship()
    encounter: Mapped[Encounter | None] = relationship(back_populates="student_notes")


class PromptTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "prompt_templates"

    key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    task_type: Mapped[AITaskType] = mapped_column(
        enum_type(AITaskType, "prompt_task_type_enum"), nullable=False
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    versions: Mapped[list[PromptVersion]] = relationship(
        back_populates="template", cascade="all, delete-orphan"
    )


class PromptVersion(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "prompt_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "version", name="uq_prompt_template_version"),
    )

    template_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("prompt_templates.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    available_context: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    retrieval_policy: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    template: Mapped[PromptTemplate] = relationship(back_populates="versions")


class AIAnalysis(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "ai_analyses"
    __table_args__ = (
        Index("ix_ai_analyses_patient_created", "patient_id", "created_at"),
        Index("ix_ai_analyses_encounter_created", "encounter_id", "created_at"),
    )

    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=True
    )
    encounter_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("encounters.id", ondelete="RESTRICT"), nullable=True
    )
    task_type: Mapped[AITaskType] = mapped_column(
        enum_type(AITaskType, "ai_analysis_task_type_enum"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    prompt_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("prompt_versions.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    source_encounter_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_chunk_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    output: Mapped[str] = mapped_column(Text, nullable=False)
    structured_output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    input_context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    deidentification_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="completed", nullable=False)

    encounter: Mapped[Encounter | None] = relationship(back_populates="analyses")
    prompt_version: Mapped[PromptVersion] = relationship()


class AIAnalysisSourceDocument(Base):
    __tablename__ = "ai_analysis_source_documents"

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("ai_analyses.id", ondelete="CASCADE"), primary_key=True
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), primary_key=True
    )


class ApprenticeshipNoteVersion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Phase 9 apprenticeship writing: one immutable note version.

    Versions are append-only rows grouped by ``note_id`` (regenerations and
    edits never overwrite an earlier version). ``version_type`` separates the
    permanently retained user_original / ai_draft / user_draft / user_final
    layers, so AI text can never be mistaken for the user's own words. AI
    drafts additionally keep the validated structured output, provider/model,
    prompt version and the source context hash; user versions carry the same
    evidence encounters so "内容依据" stays visible after editing.
    """

    __tablename__ = "apprenticeship_note_versions"
    __table_args__ = (
        UniqueConstraint("note_id", "version", name="uq_apprenticeship_note_version"),
        Index("ix_note_versions_patient_created", "patient_id", "created_at"),
        Index("ix_note_versions_note_version", "note_id", "version"),
    )

    note_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    patient_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=False
    )
    note_type: Mapped[ApprenticeshipNoteType] = mapped_column(
        enum_type(ApprenticeshipNoteType, "apprenticeship_note_type_enum"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    version_type: Mapped[NoteVersionType] = mapped_column(
        enum_type(NoteVersionType, "note_version_type_enum"), nullable=False
    )
    title: Mapped[str | None] = mapped_column(String(160), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Validated strict AI schema output; None for user-authored versions.
    structured_output: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evidence_encounter_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    source_context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 依据快照：该版本实际采用的写作材料（去标识上下文全文 + 各版本号），
    # 使依据可重建而非只剩哈希。历史版本为 NULL 且永不按当前资料回填。
    evidence_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    deidentification_version: Mapped[str] = mapped_column(String(32), nullable=False)
    based_on_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("apprenticeship_note_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )


class AuditEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_entity", "entity_type", "entity_id", "created_at"),
        Index("ix_audit_events_patient", "patient_id", "created_at"),
    )

    actor_type: Mapped[str] = mapped_column(String(40), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("patients.id", ondelete="RESTRICT"), nullable=True
    )
    before_redacted: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_redacted: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), default=uuid.uuid4, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class ProviderSetting(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Runtime-configurable external provider credentials (single slot).

    Stores the user's structured-output AI provider configuration. The API
    key is encrypted at rest with the same field-level PII encryption as
    patient identity; it is never returned by any API response.
    """

    __tablename__ = "provider_settings"

    slot: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    adapter: Mapped[str] = mapped_column(String(40), nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    api_key_encrypted: Mapped[str] = mapped_column(EncryptedText, nullable=False)
    confirmed_identifiable_upload: Mapped[bool] = mapped_column(Boolean, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class VisionVerification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Phase 6.2 Vision Verification Fallback record.

    One row per constrained image re-read performed for OCR-source
    incompleteness. The row preserves the image reference/hash, the exact
    requested fields, the complete provider response, and the sanitized
    applied fields with ``source=ai_visual_verification`` provenance. It is
    evidence, not a clinical fact: the patched values still pass through the
    same validator and resolution chain.
    """

    __tablename__ = "vision_verifications"
    __table_args__ = (
        Index("ix_vision_verifications_document", "document_id"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("documents.id", ondelete="RESTRICT"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    image_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    image_reference: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # The validator blocking codes that made this document vision-eligible.
    issue_codes: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    requested_fields: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, nullable=False
    )
    # Complete provider response (contains read values); kept like OCR raw
    # provider output, never logged.
    response: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    applied_fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    # applied | unresolvable | rejected
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
