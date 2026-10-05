from __future__ import annotations

import contextlib
import hashlib
import logging
import mimetypes
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import utcnow
from app.models.domain import (
    Diagnosis,
    Document,
    Encounter,
    EncounterDocument,
    ImportBatch,
    ImportItem,
    OCRProviderSubmission,
    Prescription,
    PrescriptionItem,
    ProcessingJob,
    ReviewItem,
    SymptomObservation,
    UnresolvedIngestion,
    VisionVerification,
)
from app.models.enums import (
    DiagnosisType,
    DoseScope,
    DoseSource,
    JobStatus,
    ProcessingStatus,
    ReviewStatus,
    Sex,
    SourceType,
)
from app.services.ai.extractor import (
    STRUCTURING_SOURCE_AI,
    ClinicalExtractionRejected,
    extraction_to_parsed_document,
    get_clinical_extraction_provider,
    validate_clinical_extraction,
)
from app.services.ai.vision_verifier import (
    SOURCE_AI_VISUAL_VERIFICATION,
    VISION_ELIGIBLE_ISSUE_CODES,
    VisionVerificationProviderError,
    VisionVerificationRejected,
    apply_vision_verifications,
    build_vision_field_requests,
    get_vision_verification_provider,
    parse_vision_verification_response,
)
from app.services.audit import append_audit_event
from app.services.calibration import has_suspicious_prescription_residual
from app.services.drug_candidates import upsert_candidate
from app.services.drugs import (
    DrugNormalizationResult,
    calculate_dose,
    find_effective_specification,
    raw_identity_verifiable,
    resolve_drug_with_suggestion,
)
from app.services.legacy_structuring import (
    STRUCTURING_SOURCE_LLM,
    LegacyStructuringRejected,
    get_legacy_structuring_provider,
    validate_legacy_structuring,
)
from app.services.ocr import OCRProviderError, get_ocr_provider
from app.services.parsers.detector import (
    CURRENT_TEMPLATE,
    LEGACY_TEMPLATE,
    UNKNOWN_TEMPLATE,
    detect_template,
)
from app.services.parsers.jnu import JNUHospitalOutpatientParser, JNUParserError
from app.services.parsers.jnu_legacy import (
    CANONICAL_HOSPITAL as LEGACY_CANONICAL_HOSPITAL,
)
from app.services.parsers.jnu_legacy import JNULegacyParserError, JNULegacyPrescriptionParser
from app.services.parsers.types import ParsedDiagnosis, ParsedDocument, ParsedPrescriptionItem
from app.services.patient_matching import PatientMatcher, PatientMatchReview
from app.services.storage import get_storage
from app.services.validation_policy import (
    VALIDATION_POLICY_VERSION,
    classify_review_issue,
    decide_ingestion,
)

logger = logging.getLogger(__name__)

# Structuring sources that must never be stored/displayed as physician records.
_AI_STRUCTURING_SOURCES = {STRUCTURING_SOURCE_LLM, STRUCTURING_SOURCE_AI}

TERMINAL_STATUSES = {
    ProcessingStatus.ACCEPTED,
    ProcessingStatus.REVIEW_REQUIRED,
    ProcessingStatus.UNRESOLVED,
}

# Field names recorded on the unresolved ledger per blocking reason code.
_CRITICAL_FIELDS_BY_REASON = {
    "PATIENT_NAME_MISSING": ("patient_identity.real_name",),
    "MEDICAL_RECORD_NUMBER_MISSING": ("patient_identity.medical_record_number", "patient_match"),
    "UNLABELED_HEADER_MRN_INFERRED": ("patient_identity.medical_record_number", "patient_match"),
    "PATIENT_MATCH_REVIEW_REQUIRED": ("patient_match",),
    "PATIENT_IDENTITY_NAME_CONFLICT": ("patient_identity.real_name", "patient_match"),
    "PATIENT_IDENTITY_SEX_CONFLICT": ("patient.sex", "patient_match"),
    "MULTIPLE_PATIENT_NAMES_SUSPECTED": ("patient_identity.real_name", "patient_match"),
    "PACKAGE_UNIT_MISSING_IN_OCR": (
        "prescription.items.package_count",
        "prescription.items.calculated_dose_g",
    ),
    "UNPARSED_PRESCRIPTION_RESIDUAL": ("prescription.item_count",),
    "LEGACY_UNPARSED_RESIDUAL": ("prescription.item_count",),
    "PRESCRIPTION_NOT_FOUND": ("prescription.item_count",),
    "ENCOUNTER_DATE_MISSING": ("encounter.encounter_date",),
    "HOSPITAL_MISSING": ("encounter.hospital", "patient_match"),
    "DRUG_IDENTITY_UNVERIFIABLE": ("prescription.items.raw_drug_name",),
    "AI_EXTRACTION_PROVIDER_FAILED": ("document.structured_snapshot",),
    "AI_EXTRACTION_SCHEMA_REJECTED": ("document.structured_snapshot",),
    "AI_EXTRACTION_LOW_CONFIDENCE": ("document.structured_snapshot",),
    "AI_EXTRACTION_EVIDENCE_MISSING": ("prescription.items.original_text",),
    "DUPLICATED_PRESCRIPTION_ITEM": ("prescription.items",),
    "JNU_PARSER_INCOMPLETE": ("document.structured_snapshot",),
    "LEGACY_PARSER_INCOMPLETE": ("document.structured_snapshot",),
    "UNSUPPORTED_TEMPLATE": ("document.parser_template",),
    "GENERIC_PARSER_REVIEW_REQUIRED": ("document.parser_template",),
    "LLM_STRUCTURING_SCHEMA_REJECTED": ("document.structured_snapshot",),
    "LLM_STRUCTURING_LOW_CONFIDENCE": ("document.structured_snapshot",),
}


class PipelineRetryableError(RuntimeError):
    pass


@dataclass(frozen=True)
class PipelineResult:
    document_id: uuid.UUID
    status: ProcessingStatus
    patient_id: uuid.UUID | None = None
    encounter_id: uuid.UUID | None = None
    review_count: int = 0


@dataclass(frozen=True)
class DrugResolution:
    """Per-item drug resolution outcome (Phase 6.1).

    status: "normalized" (canonical resolved), "pending" (traceable raw
    name, no canonical mapping — stored with normalization_status='pending'),
    or "unverifiable" (raw identity unproven — blocks ingestion).
    """

    result: DrugNormalizationResult
    status: str


@dataclass(frozen=True)
class _AIStructuringOutcome:
    """Result of one ClinicalAIExtractor attempt (payload kept for the
    optional second-pass verification against the original OCR)."""

    parsed: ParsedDocument | None = None
    payload: dict | None = None
    failure_code: str | None = None


class PipelineService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.settings = get_settings()

    def process(self, document_id: uuid.UUID) -> PipelineResult:
        document = self.session.get(Document, document_id)
        if document is None:
            raise ValueError("Document does not exist")
        existing = self._result_if_terminal(document)
        if existing:
            return existing

        job = self._ensure_job(document)
        if not self._claim_job(job):
            return PipelineResult(document.id, document.processing_status)

        submission_id: uuid.UUID | None = None
        try:
            if document.ocr_text:
                self._set_status(document, job, ProcessingStatus.OCR_COMPLETED)
            else:
                self._set_status(document, job, ProcessingStatus.OCR_RUNNING)
                provider = get_ocr_provider()
                path = get_storage().path_for(document.original_file_path)
                submission = OCRProviderSubmission(
                    document_id=document.id,
                    processing_job_id=job.id,
                    attempt=job.attempt,
                    provider=provider.name,
                    submitted_at=utcnow(),
                    status="submitted",
                )
                self.session.add(submission)
                self.session.flush()
                submission_id = submission.id
                self.session.commit()
                result = provider.extract(
                    path,
                    explicit_text=document.processing_payload.get("mock_ocr_text"),
                    on_submitted=lambda request_id: self._record_provider_request_id(
                        submission_id, job.id, request_id
                    ),
                )
                document.ocr_provider = f"{result.provider}/{result.provider_version}"
                document.ocr_text = result.text
                document.ocr_raw_result = result.raw_result
                job.provider_task_id = result.external_task_id
                self._complete_provider_submission(submission_id, result.external_task_id)
                self._set_status(document, job, ProcessingStatus.OCR_COMPLETED)

            detection = detect_template(document.ocr_text or "")
            document.parser_template = detection.template
            self._set_status(document, job, ProcessingStatus.TEMPLATE_DETECTED)
            # Detection is informational in the AI-primary chain: it only
            # labels the document for reporting and the unresolved ledger.
            # The deterministic parsers remain legacy/fallback and regression
            # reference; both current and legacy layouts structurally go to
            # the same ClinicalAIExtractor when AI extraction is enabled.
            if (
                not self.settings.ai_extraction_enabled
                and detection.template == UNKNOWN_TEMPLATE
            ):
                return self._complete_unresolved(
                    document,
                    job,
                    stage=ProcessingStatus.TEMPLATE_DETECTED,
                    reason_code="UNSUPPORTED_TEMPLATE",
                )

            self._set_status(document, job, ProcessingStatus.PARSING)
            outcome = self._structure_document(detection.template, document)
            if outcome.failure_code is not None or outcome.parsed is None:
                return self._complete_unresolved(
                    document,
                    job,
                    stage=ProcessingStatus.PARSING,
                    reason_code=outcome.failure_code or "AI_EXTRACTION_PROVIDER_FAILED",
                )
            parsed = outcome.parsed
            document.parser_version = parsed.parser_version
            document.structured_snapshot = parsed.to_json()
            self._set_status(document, job, ProcessingStatus.STRUCTURING)

            if "MULTIPLE_PATIENT_NAMES_SUSPECTED" in parsed.warnings:
                return self._complete_unresolved(
                    document,
                    job,
                    stage=ProcessingStatus.STRUCTURING,
                    reason_code="MULTIPLE_PATIENT_NAMES_SUSPECTED",
                )

            # Validator is the safety core. Every blocking gate — identity
            # completeness, encounter date, prescription completeness, dose
            # shape, OCR residue, duplicates — plus the dictionary-gated drug
            # resolution run BEFORE any patient/encounter write, so an
            # UNRESOLVED terminal can never commit clinical rows.
            self._set_status(document, job, ProcessingStatus.VALIDATING)
            blocking_issues = self._blocking_issues(parsed)
            if (
                blocking_issues
                and self.settings.ai_extraction_enabled
                and self.settings.ai_verification_enabled
                and outcome.payload is not None
            ):
                # Optional configurable second pass: re-check the extraction
                # against the original OCR only — never free medical
                # inference. One attempt, then the validator decides again.
                second = self._second_pass_verification(
                    document.ocr_text or "",
                    outcome.payload,
                    [issue["issue_code"] for issue in blocking_issues],
                    detection.template,
                )
                if second.failure_code is not None:
                    self._record_issue_reviews(document, parsed, blocking_issues)
                    return self._complete_unresolved(
                        document,
                        job,
                        stage=ProcessingStatus.VALIDATING,
                        reason_code=second.failure_code,
                    )
                if second.parsed is not None:
                    parsed = second.parsed
                    document.structured_snapshot = parsed.to_json()
                    blocking_issues = self._blocking_issues(parsed)

            # Phase 6.2 Vision Verification Fallback: when blocking issues
            # remain and they mean the OCR SOURCE lost information, the
            # original image may be re-read once, constrained to exactly the
            # missing fields. Patched values carry
            # source=ai_visual_verification provenance and re-enter the SAME
            # validator plus the untouched dictionary/dose/matching chain;
            # unclear or rejected reads leave the document unresolved.
            if (
                blocking_issues
                and self.settings.ai_extraction_enabled
                and outcome.payload is not None
            ):
                parsed = self._vision_verification_fallback(document, parsed, blocking_issues)
                if parsed.vision_verifications:
                    document.structured_snapshot = parsed.to_json()
                blocking_issues = self._blocking_issues(parsed)

            drug_resolutions: dict[int, DrugResolution] | None = None
            # Candidate accumulation runs before the other blocking gates so
            # the dictionary bootstrap stays complete across cases.
            drug_resolutions, drug_issues = self._resolve_prescription_drugs(
                parsed, document_id=document.id
            )
            if drug_issues:
                self._record_issue_reviews(document, parsed, drug_issues)
                return self._complete_unresolved(
                    document,
                    job,
                    stage=ProcessingStatus.VALIDATING,
                    reason_code=drug_issues[0]["issue_code"],
                )

            if blocking_issues:
                self._record_issue_reviews(document, parsed, blocking_issues)
                return self._complete_unresolved(
                    document,
                    job,
                    stage=ProcessingStatus.VALIDATING,
                    reason_code=blocking_issues[0]["issue_code"],
                )

            self._set_status(document, job, ProcessingStatus.PATIENT_MATCHING)
            try:
                match = PatientMatcher(self.session).match_or_create(
                    parsed, evidence_document_id=document.id
                )
            except PatientMatchReview as exc:
                self.session.rollback()
                document = self.session.get(Document, document_id)
                refetched_job = self._get_job(document_id)
                assert document is not None and refetched_job is not None
                job = refetched_job
                return self._complete_unresolved(
                    document,
                    job,
                    stage=ProcessingStatus.PATIENT_MATCHING,
                    reason_code=exc.code,
                )

            # Patient creation/match and all Encounter facts commit atomically below.
            self._set_status(document, job, ProcessingStatus.NORMALIZING, commit=False)
            existing_encounter = self.session.scalar(
                select(Encounter).where(Encounter.primary_document_id == document.id)
            )
            if existing_encounter is not None:
                return self._finish_existing(document, job, existing_encounter)

            assert drug_resolutions is not None
            encounter = self._create_domain(document, parsed, match.patient, drug_resolutions)
            self._set_status(document, job, ProcessingStatus.VALIDATING, commit=False)
            self._record_issue_reviews(document, parsed, self._document_issues(parsed), encounter)
            open_blocking = self._open_blocking_review_count(document.id)
            terminal = ProcessingStatus.UNRESOLVED if open_blocking else ProcessingStatus.ACCEPTED
            if terminal == ProcessingStatus.UNRESOLVED:
                # Defense in depth: _create_domain only flushed (never
                # committed) clinical rows. An UNRESOLVED terminal must never
                # persist them, so discard the pending clinical write before
                # recording the unresolved ledger.
                reason_code = self._open_blocking_reason(document.id)
                self.session.rollback()
                document = self.session.get(Document, document_id)
                refetched_job = self._get_job(document_id)
                assert document is not None and refetched_job is not None
                job = refetched_job
                return self._complete_unresolved(
                    document,
                    job,
                    stage=ProcessingStatus.VALIDATING,
                    reason_code=reason_code,
                )
            document.processing_payload = {}
            document.processing_status = terminal
            job.stage = terminal
            job.status = JobStatus.COMPLETED
            job.finished_at = utcnow()
            self._mark_unresolved_resolved(document.id, "auto_accepted")
            self._sync_import_item(document)
            self._update_batch(document)
            append_audit_event(
                self.session,
                action="pipeline.completed",
                entity_type="document",
                entity_id=document.id,
                patient_id=match.patient.id,
                after_redacted={
                    "status": terminal.value,
                    "encounter_id": str(encounter.id),
                    "review_count": self._open_review_count(document.id),
                    "validation_policy_version": VALIDATION_POLICY_VERSION,
                },
            )
            self.session.commit()
            return PipelineResult(
                document.id,
                terminal,
                patient_id=match.patient.id,
                encounter_id=encounter.id,
                review_count=self._open_review_count(document.id),
            )
        except OCRProviderError as exc:
            self.session.rollback()
            self._fail_provider_submission(submission_id, exc.code)
            self._record_failure(document_id, exc.code, retryable=exc.retryable)
            if exc.retryable:
                raise PipelineRetryableError(exc.code) from exc
            return PipelineResult(document_id, ProcessingStatus.FAILED)
        except Exception:
            self.session.rollback()
            self._fail_provider_submission(submission_id, "PIPELINE_UNEXPECTED")
            self._record_failure(document_id, "PIPELINE_UNEXPECTED", retryable=False)
            logger.exception(
                "Pipeline failed",
                extra={"document_id": str(document_id), "error_code": "PIPELINE_UNEXPECTED"},
            )
            raise

    def _parse_for_template(
        self, template: str, document: Document
    ) -> tuple[ParsedDocument | None, str | None]:
        """Run exactly the parser owning this template; never cross-hack."""

        ocr_text = document.ocr_text or ""
        if template == CURRENT_TEMPLATE:
            try:
                return JNUHospitalOutpatientParser().parse(ocr_text), None
            except JNUParserError:
                return None, "JNU_PARSER_INCOMPLETE"
        if template == LEGACY_TEMPLATE:
            try:
                return JNULegacyPrescriptionParser().parse(ocr_text), None
            except JNULegacyParserError:
                return self._legacy_structured_or_failure(ocr_text)
        return None, "UNSUPPORTED_TEMPLATE"

    def _structure_document(
        self, template: str, document: Document
    ) -> _AIStructuringOutcome:
        """AI-primary structuring; deterministic parsers stay as fallback."""

        if self.settings.ai_extraction_enabled:
            return self._structure_with_ai(template, document)
        parsed, failure_code = self._parse_for_template(template, document)
        return _AIStructuringOutcome(parsed=parsed, failure_code=failure_code)

    def _structure_with_ai(self, template: str, document: Document) -> _AIStructuringOutcome:
        """Unified ClinicalAIExtractor path for current and legacy layouts.

        The provider payload must pass the strict schema, confidence floor,
        and OCR-evidence gates before it becomes candidate facts; the AI
        never writes to the database directly.
        """

        ocr_text = document.ocr_text or ""
        try:
            provider = get_clinical_extraction_provider()
            payload = provider.extract(ocr_text)
            extraction = validate_clinical_extraction(payload, ocr_text)
        except ClinicalExtractionRejected as exc:
            return _AIStructuringOutcome(failure_code=exc.issue_code)
        parsed = extraction_to_parsed_document(
            extraction,
            template=template,
            parser_version=f"ai-clinical/{self.settings.ai_extractor_version}",
        )
        return _AIStructuringOutcome(parsed=parsed, payload=payload)

    def _second_pass_verification(
        self,
        ocr_text: str,
        payload: dict,
        issue_codes: list[str],
        template: str,
    ) -> _AIStructuringOutcome:
        """One verification call against the original OCR after blocking issues."""

        try:
            provider = get_clinical_extraction_provider()
            corrected = provider.verify(ocr_text, payload, issue_codes)
            if corrected is None:
                return _AIStructuringOutcome()
            extraction = validate_clinical_extraction(corrected, ocr_text)
        except ClinicalExtractionRejected as exc:
            return _AIStructuringOutcome(failure_code=exc.issue_code)
        parsed = extraction_to_parsed_document(
            extraction,
            template=template,
            parser_version=f"ai-clinical/{self.settings.ai_extractor_version}",
        )
        return _AIStructuringOutcome(parsed=parsed, payload=corrected)

    def _vision_verification_fallback(
        self,
        document: Document,
        parsed: ParsedDocument,
        blocking_issues: list[dict],
    ) -> ParsedDocument:
        """One constrained image re-read for OCR-source incompleteness.

        Runs at most once per ingestion attempt, only for whitelisted
        blocking codes, and only when both the capability flag and the
        backend-only image-upload switch are enabled. Fail-closed on every
        path: an unclear read, a contract violation, or a provider error
        leaves the blocking issues untouched (terminal UNRESOLVED).
        """

        issue_codes = [issue["issue_code"] for issue in blocking_issues]
        eligible = [code for code in issue_codes if code in VISION_ELIGIBLE_ISSUE_CODES]
        if not eligible:
            return parsed
        provider = get_vision_verification_provider()
        if provider is None:
            return parsed
        requests = build_vision_field_requests(parsed, eligible)
        if not requests:
            return parsed

        provider_name = getattr(provider, "name", "unknown")
        provider_model = self.settings.ai_vision_provider_model
        describe = getattr(provider, "describe", None)
        if describe is not None:
            with contextlib.suppress(Exception):  # record metadata only
                provider_name, provider_model = describe()

        image_path = Path(get_storage().path_for(document.original_file_path))
        image_bytes = image_path.read_bytes()
        media_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
        requested = [
            {"field_path": request.field_path, "kind": request.kind} for request in requests
        ]
        record = VisionVerification(
            document_id=document.id,
            attempt=1,
            provider=provider_name,
            model=provider_model,
            image_sha256=hashlib.sha256(image_bytes).hexdigest(),
            image_reference=document.original_file_path,
            issue_codes=eligible,
            requested_fields=requested,
            response={},
            applied_fields=[],
            status="unresolvable",
            source=SOURCE_AI_VISUAL_VERIFICATION,
        )
        self.session.add(record)
        applied_fields: list[dict] = []
        try:
            response_payload = provider.verify_fields(image_bytes, media_type, requests)
            record.response = response_payload
            verification = parse_vision_verification_response(response_payload)
            parsed, applied_fields = apply_vision_verifications(
                parsed, requests, verification
            )
            record.applied_fields = applied_fields
            record.status = (
                "applied" if any(f["applied"] for f in applied_fields) else "unresolvable"
            )
        except VisionVerificationRejected as exc:
            record.status = "rejected"
            logger.info(
                "vision verification rejected",
                extra={"document_id": str(document.id), "reason_code": str(exc)[:120]},
            )
        except VisionVerificationProviderError as exc:
            record.status = "provider_error"
            logger.info(
                "vision verification provider error",
                extra={"document_id": str(document.id), "reason_code": str(exc)[:120]},
            )
        append_audit_event(
            self.session,
            action="pipeline.vision_verification",
            entity_type="document",
            entity_id=document.id,
            after_redacted={
                "status": record.status,
                "issue_codes": eligible,
                "requested_fields": [entry["field_path"] for entry in requested],
                "applied_fields": [
                    entry["field_path"] for entry in applied_fields if entry["applied"]
                ],
                "provider": provider_name,
                "model": provider_model,
                "image_sha256": record.image_sha256,
                "source": SOURCE_AI_VISUAL_VERIFICATION,
            },
        )
        return parsed

    def _resolve_prescription_drugs(
        self, parsed: ParsedDocument, document_id: uuid.UUID
    ) -> tuple[dict[int, DrugResolution], list[dict]]:
        """Dictionary-gated identity resolution (Phase 6.2 semantics).

        A traceable raw name without a canonical mapping is a coverage gap:
        the item is stored as ``normalization_status='pending'`` and a
        PENDING candidate is created/reused (usage + evidence accumulate) so
        the user confirms each new herb exactly once. Blocking is reserved
        for items whose raw identity itself is unproven — not traceable to
        the cited OCR evidence, or suspected garbled/non-drug.

        Runs even when other blocking issues exist: candidates are reference
        data, so the bootstrap list stays complete across cases.
        """

        resolutions: dict[int, DrugResolution] = {}
        blocking: list[dict] = []
        for index, item in enumerate(parsed.prescription_items):
            resolved = resolve_drug_with_suggestion(
                self.session,
                item.raw_drug_name,
                item.processing_method,
                item.suggested_canonical_name,
            )
            if resolved.dictionary is not None:
                resolutions[index] = DrugResolution(resolved, "normalized")
                continue
            if not raw_identity_verifiable(item.raw_drug_name, item.original_text):
                resolutions[index] = DrugResolution(resolved, "unverifiable")
                blocking.append(
                    _issue(
                        "DRUG_IDENTITY_UNVERIFIABLE", f"prescription.items[{index}].raw_drug_name"
                    )
                )
                continue
            resolutions[index] = DrugResolution(resolved, "pending")
            upsert_candidate(
                self.session,
                raw_name=item.raw_drug_name,
                suggested_canonical=item.suggested_canonical_name,
                suggested_processing=item.processing_method,
                evidence_text=item.original_text,
                source_document_id=document_id,
            )
        return resolutions, blocking

    def _legacy_structured_or_failure(
        self, ocr_text: str
    ) -> tuple[ParsedDocument | None, str | None]:
        """LLM-assisted legacy structuring; disabled unless explicitly enabled.

        The structured payload re-enters the same validation gates below;
        provider confidence alone never produces an auto-accept.
        """

        provider = get_legacy_structuring_provider()
        if provider is None:
            return None, "LEGACY_PARSER_INCOMPLETE"
        try:
            structured = validate_legacy_structuring(provider.structure(ocr_text))
        except LegacyStructuringRejected as exc:
            return None, exc.issue_code
        items = [
            ParsedPrescriptionItem(
                original_text=item.original_text,
                is_basic_drug=False,
                raw_drug_name=item.raw_drug_name,
                processing_method=item.processing_method,
                explicit_package_dose_g=item.explicit_dose_g,
                package_count=item.package_count,
                original_package_unit=item.package_unit,
                normalized_package_unit="package" if item.package_unit else None,
                is_direct_gram_dose=item.package_count is None,
            )
            for item in structured.items
        ]
        warnings: list[str] = []
        if structured.real_name is None:
            warnings.append("PATIENT_NAME_MISSING")
        if structured.medical_record_number is None:
            warnings.append("MEDICAL_RECORD_NUMBER_MISSING")
        if structured.sex == "unknown":
            warnings.append("SEX_UNKNOWN")
        parsed = ParsedDocument(
            template=LEGACY_TEMPLATE,
            parser_version=self.settings.jnu_legacy_parser_version,
            hospital=LEGACY_CANONICAL_HOSPITAL,
            real_name=structured.real_name,
            sex=Sex(structured.sex),
            age=structured.age,
            encounter_date=structured.encounter_date,
            medical_record_number=structured.medical_record_number,
            department=structured.department,
            physician=structured.physician,
            chief_complaint=None,
            present_illness=None,
            past_history=None,
            allergy=None,
            vital_signs=None,
            advice=None,
            treatment_record=None,
            diagnoses=[
                ParsedDiagnosis(
                    raw=value,
                    normalized=value,
                    diagnosis_type=DiagnosisType.WESTERN,
                    is_primary=False,
                )
                for value in structured.diagnoses
            ],
            prescription_items=items,
            total_decoctions=structured.total_decoctions,
            warnings=warnings,
            structuring_source=STRUCTURING_SOURCE_LLM,
        )
        return parsed, None

    def _document_issues(self, parsed: ParsedDocument) -> list[dict]:
        """All decision-relevant issues computable from the parsed document."""

        issues: list[dict] = []
        if parsed.real_name is None:
            issues.append(_issue("PATIENT_NAME_MISSING", "patient_identity.real_name"))
        if parsed.medical_record_number is None:
            issues.append(
                _issue("MEDICAL_RECORD_NUMBER_MISSING", "patient_identity.medical_record_number")
            )
        elif "UNLABELED_HEADER_MRN_INFERRED" in parsed.warnings:
            issues.append(
                _issue("UNLABELED_HEADER_MRN_INFERRED", "patient_identity.medical_record_number")
            )
        if not parsed.hospital.strip():
            issues.append(_issue("HOSPITAL_MISSING", "encounter.hospital"))
        if parsed.encounter_date is None:
            issues.append(_issue("ENCOUNTER_DATE_MISSING", "encounter.encounter_date"))
        if parsed.sex == Sex.UNKNOWN:
            issues.append(_issue("SEX_UNKNOWN", "patient.sex"))
        if parsed.age is not None and not 0 <= parsed.age <= 130:
            issues.append(_issue("AGE_OUT_OF_RANGE", "encounter.age_at_encounter"))
        if not parsed.prescription_items:
            issues.append(_issue("PRESCRIPTION_NOT_FOUND", "encounter.prescriptions"))
        if "MULTIPLE_PATIENT_NAMES_SUSPECTED" in parsed.warnings:
            issues.append(_issue("MULTIPLE_PATIENT_NAMES_SUSPECTED", "patient_identity.real_name"))
        if has_suspicious_prescription_residual(parsed.raw_instruction_text):
            issues.append(
                _issue("UNPARSED_PRESCRIPTION_RESIDUAL", "prescription.unparsed_residual")
            )
        if "LEGACY_UNPARSED_RESIDUAL" in parsed.warnings:
            issues.append(_issue("LEGACY_UNPARSED_RESIDUAL", "prescription.unparsed_residual"))
        seen_item_keys: dict[tuple, int] = {}
        for index, parsed_item in enumerate(parsed.prescription_items):
            if parsed_item.package_count is not None and parsed_item.original_package_unit is None:
                issues.append(
                    _issue(
                        "PACKAGE_UNIT_MISSING_IN_OCR", f"prescription.items[{index}].package_count"
                    )
                )
            item_key = (
                parsed_item.raw_drug_name,
                parsed_item.processing_method,
                str(parsed_item.explicit_package_dose_g),
                str(parsed_item.package_count),
                parsed_item.original_package_unit,
            )
            if item_key in seen_item_keys:
                issues.append(
                    _issue("DUPLICATED_PRESCRIPTION_ITEM", f"prescription.items[{index}]")
                )
            else:
                seen_item_keys[item_key] = index
        return issues

    def _blocking_issues(self, parsed: ParsedDocument) -> list[dict]:
        issues = self._document_issues(parsed)
        decision = decide_ingestion(issue["issue_code"] for issue in issues)
        if decision.decision == "auto_accept":
            return []
        blocking_codes = set(decision.blocking_reasons)
        return [issue for issue in issues if issue["issue_code"] in blocking_codes]

    def _create_domain(
        self,
        document: Document,
        parsed: ParsedDocument,
        patient,
        drug_resolutions: dict[int, DrugResolution],
    ) -> Encounter:
        if parsed.encounter_date is None:
            # Unreachable: ENCOUNTER_DATE_MISSING blocks before domain creation.
            raise ValueError("Encounter date missing after validation")
        encounter_at = datetime.combine(
            parsed.encounter_date,
            time.min,
            tzinfo=ZoneInfo(self.settings.app_timezone),
        ).astimezone(UTC)
        # AI-structured encounters carry ai_normalized provenance on every
        # clinical assertion; the deterministic-parser path stays
        # physician_record. AI output is never presented as a physician record.
        ai_structured = parsed.structuring_source in _AI_STRUCTURING_SOURCES
        encounter = Encounter(
            patient_id=patient.id,
            primary_document_id=document.id,
            hospital=parsed.hospital,
            department=parsed.department,
            physician=parsed.physician,
            encounter_at=encounter_at,
            age_at_encounter=parsed.age
            if parsed.age is not None and 0 <= parsed.age <= 130
            else None,
            chief_complaint_raw=parsed.chief_complaint,
            present_illness_raw=parsed.present_illness,
            past_history_raw=parsed.past_history,
            allergy_raw=parsed.allergy,
            vital_signs_raw=parsed.vital_signs,
            advice_raw=parsed.advice,
            treatment_record_raw=parsed.treatment_record,
            raw_source_type=(
                SourceType.AI_NORMALIZED if ai_structured else SourceType.PHYSICIAN_RECORD
            ),
        )
        self.session.add(encounter)
        self.session.flush()
        self.session.add(
            EncounterDocument(encounter_id=encounter.id, document_id=document.id, role="primary")
        )

        for parsed_symptom in parsed.symptoms:
            self.session.add(
                SymptomObservation(
                    encounter_id=encounter.id,
                    symptom_raw=parsed_symptom.raw,
                    symptom_normalized=parsed_symptom.normalized,
                    status=parsed_symptom.status,
                    trend=parsed_symptom.trend,
                    source_type=(
                        SourceType.AI_NORMALIZED if ai_structured else SourceType.PHYSICIAN_RECORD
                    ),
                    confidence=Decimal("0.9500"),
                    evidence_document_id=document.id,
                    normalization_version=parsed.parser_version,
                )
            )
        for parsed_diagnosis in parsed.diagnoses:
            self.session.add(
                Diagnosis(
                    encounter_id=encounter.id,
                    diagnosis_type=parsed_diagnosis.diagnosis_type,
                    name_raw=parsed_diagnosis.raw,
                    name_normalized=parsed_diagnosis.normalized,
                    is_primary=parsed_diagnosis.is_primary,
                    source_type=(
                        SourceType.AI_NORMALIZED if ai_structured else SourceType.PHYSICIAN_RECORD
                    ),
                    confidence=Decimal("0.9700"),
                    evidence_document_id=document.id,
                    normalization_version=parsed.parser_version,
                )
            )

        if parsed.prescription_items or parsed.raw_instruction_text or parsed.total_decoctions:
            prescription = Prescription(
                encounter_id=encounter.id,
                evidence_document_id=document.id,
                total_decoctions=parsed.total_decoctions,
                raw_instruction_text=parsed.raw_instruction_text,
                source_type=(
                    SourceType.AI_NORMALIZED if ai_structured else SourceType.PHYSICIAN_RECORD
                ),
            )
            self.session.add(prescription)
            self.session.flush()
            for line_number, parsed_item in enumerate(parsed.prescription_items, start=1):
                # Identity was resolved pre-commit via the DrugDictionary;
                # only "normalized" and "pending" items reach this loop —
                # "unverifiable" raw identity blocks the whole document.
                resolution = drug_resolutions[line_number - 1]
                normalized = resolution.result
                pending = resolution.status == "pending"
                specification = None
                dose_source = DoseSource.UNKNOWN
                if parsed_item.explicit_package_dose_g is not None:
                    dose_source = DoseSource.EXPLICIT
                elif normalized.dictionary is not None and parsed_item.original_package_unit:
                    specification = find_effective_specification(
                        self.session,
                        hospital=parsed.hospital,
                        dictionary_id=normalized.dictionary.id,
                        processing_method=normalized.processing_method,
                        normalized_package_unit=parsed_item.normalized_package_unit or "package",
                        encounter_date=parsed.encounter_date,
                    )
                    if specification is not None:
                        dose_source = DoseSource.HOSPITAL_SPEC
                calculated = calculate_dose(
                    parsed_item.explicit_package_dose_g,
                    parsed_item.package_count,
                    specification,
                )
                if calculated is None and parsed_item.is_direct_gram_dose:
                    calculated = parsed_item.explicit_package_dose_g
                item = PrescriptionItem(
                    prescription_id=prescription.id,
                    line_number=line_number,
                    original_text=parsed_item.original_text,
                    is_basic_drug=parsed_item.is_basic_drug,
                    raw_drug_name=parsed_item.raw_drug_name,
                    drug_dictionary_id=(
                        normalized.dictionary.id if normalized.dictionary else None
                    ),
                    canonical_drug_name=(
                        normalized.canonical_name if normalized.dictionary else None
                    ),
                    ai_suggested_canonical_name=(
                        parsed_item.suggested_canonical_name if pending else None
                    ),
                    normalization_status="pending" if pending else "normalized",
                    processing_method=normalized.processing_method,
                    explicit_package_dose_g=parsed_item.explicit_package_dose_g,
                    package_count=parsed_item.package_count,
                    original_package_unit=parsed_item.original_package_unit,
                    normalized_package_unit=parsed_item.normalized_package_unit,
                    calculated_dose_g=calculated,
                    dose_source=dose_source,
                    dose_scope=DoseScope.PER_DECOCTION,
                    hospital_specification_id=(specification.id if specification else None),
                    parser_version=parsed.parser_version,
                    normalization_version=self.settings.drug_dictionary_version,
                    confidence=normalized.confidence,
                    review_status=ReviewStatus.RESOLVED,
                )
                self.session.add(item)
                self.session.flush()
                if pending:
                    item.review_status = ReviewStatus.OPEN
                    self._review(
                        document=document,
                        encounter_id=encounter.id,
                        prescription_item_id=item.id,
                        issue_code="UNKNOWN_DRUG",
                        field_path=f"prescription.items[{line_number - 1}].raw_drug_name",
                        observed={
                            "original_text": parsed_item.original_text,
                            "ai_suggested_canonical_name": parsed_item.suggested_canonical_name,
                        },
                    )
                if (
                    parsed_item.package_count is not None
                    and parsed_item.original_package_unit is None
                ):
                    item.review_status = ReviewStatus.OPEN
                    self._review(
                        document=document,
                        encounter_id=encounter.id,
                        prescription_item_id=item.id,
                        issue_code="PACKAGE_UNIT_MISSING_IN_OCR",
                        field_path=f"prescription.items[{line_number - 1}].package_count",
                        observed={
                            "package_count_candidate": str(parsed_item.package_count),
                            "original_package_unit": None,
                        },
                    )
                if (
                    parsed_item.explicit_package_dose_g is None
                    and parsed_item.package_count is not None
                    and specification is None
                ):
                    item.review_status = ReviewStatus.OPEN
                    self._review(
                        document=document,
                        encounter_id=encounter.id,
                        prescription_item_id=item.id,
                        issue_code="HOSPITAL_SPECIFICATION_UNKNOWN",
                        field_path=f"prescription.items[{line_number - 1}].calculated_dose_g",
                        observed={
                            "drug": normalized.canonical_name or parsed_item.raw_drug_name,
                            "package_count": str(parsed_item.package_count),
                            "original_package_unit": parsed_item.original_package_unit,
                        },
                    )

        patient.first_encounter_at = (
            encounter_at
            if patient.first_encounter_at is None
            else min(patient.first_encounter_at, encounter_at)
        )
        patient.last_encounter_at = (
            encounter_at
            if patient.last_encounter_at is None
            else max(patient.last_encounter_at, encounter_at)
        )
        patient.row_version += 1
        self.session.flush()
        return encounter

    def _record_issue_reviews(
        self,
        document: Document,
        parsed: ParsedDocument,
        issues: list[dict],
        encounter: Encounter | None = None,
    ) -> None:
        for issue in issues:
            self._review(
                document=document,
                encounter_id=encounter.id if encounter is not None else None,
                issue_code=issue["issue_code"],
                field_path=issue["field_path"],
                observed={"phase": "5.2_policy"},
            )

    def _ensure_job(self, document: Document) -> ProcessingJob:
        key = f"{document.id}:{self.settings.pipeline_version}"
        job = self.session.scalar(select(ProcessingJob).where(ProcessingJob.idempotency_key == key))
        if job is None:
            job = ProcessingJob(
                document_id=document.id,
                pipeline_version=self.settings.pipeline_version,
                stage=ProcessingStatus.QUEUED,
                status=JobStatus.PENDING,
                idempotency_key=key,
            )
            self.session.add(job)
            document.processing_status = ProcessingStatus.QUEUED
            self._sync_import_item(document)
            self.session.commit()
        return job

    def _record_provider_request_id(
        self,
        submission_id: uuid.UUID,
        job_id: uuid.UUID,
        request_id: str,
    ) -> None:
        submission = self.session.get(OCRProviderSubmission, submission_id)
        job = self.session.get(ProcessingJob, job_id)
        if submission is None or job is None:
            raise RuntimeError("OCR provider submission audit row is missing")
        submission.provider_request_id = request_id
        submission.status = "running"
        job.provider_task_id = request_id
        self.session.commit()

    def _complete_provider_submission(
        self,
        submission_id: uuid.UUID,
        request_id: str | None,
    ) -> None:
        submission = self.session.get(OCRProviderSubmission, submission_id)
        if submission is None:
            raise RuntimeError("OCR provider submission audit row is missing")
        submission.provider_request_id = request_id or submission.provider_request_id
        submission.status = "completed"
        submission.completed_at = utcnow()

    def _fail_provider_submission(self, submission_id: uuid.UUID | None, code: str) -> None:
        if submission_id is None:
            return
        submission = self.session.get(OCRProviderSubmission, submission_id)
        if submission is None:
            return
        submission.status = "failed"
        submission.failure_code = code
        submission.completed_at = utcnow()
        self.session.commit()

    def _claim_job(self, job: ProcessingJob) -> bool:
        result = cast("CursorResult[Any]", self.session.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.id == job.id,
                ProcessingJob.status.in_([JobStatus.PENDING, JobStatus.RETRY_SCHEDULED]),
            )
            .values(
                status=JobStatus.RUNNING,
                started_at=utcnow(),
                attempt=ProcessingJob.attempt + 1,
                error_code=None,
                error_detail_redacted=None,
            )
        )
        )
        self.session.commit()
        self.session.refresh(job)
        return bool(result.rowcount)

    def _set_status(
        self,
        document: Document,
        job: ProcessingJob,
        status: ProcessingStatus,
        *,
        commit: bool = True,
    ) -> None:
        document.processing_status = status
        job.stage = status
        self._sync_import_item(document)
        if commit:
            self.session.commit()

    def _review(
        self,
        *,
        document: Document,
        issue_code: str,
        field_path: str,
        observed: dict,
        encounter_id: uuid.UUID | None = None,
        prescription_item_id: uuid.UUID | None = None,
    ) -> ReviewItem:
        existing = self.session.scalar(
            select(ReviewItem).where(
                ReviewItem.document_id == document.id,
                ReviewItem.issue_code == issue_code,
                ReviewItem.field_path == field_path,
                ReviewItem.status == ReviewStatus.OPEN,
            )
        )
        if existing:
            return existing
        rule = classify_review_issue(issue_code)
        item = ReviewItem(
            document_id=document.id,
            encounter_id=encounter_id,
            prescription_item_id=prescription_item_id,
            issue_code=issue_code,
            field_path=field_path,
            severity="critical" if rule.blocking else "warning",
            reason_category=rule.category,
            criticality=rule.criticality,
            blocking=rule.blocking,
            observed_value=observed,
            status=ReviewStatus.OPEN,
        )
        self.session.add(item)
        self.session.flush()
        return item

    def _complete_unresolved(
        self,
        document: Document,
        job: ProcessingJob,
        *,
        stage: ProcessingStatus,
        reason_code: str,
    ) -> PipelineResult:
        """Terminal UNRESOLVED: no committed clinical rows, no manual queue.

        The immutable source document, OCR output, and structured snapshot
        stay preserved; an unresolved-ledger row records why ingestion
        failed closed so an explicit parser-upgrade reprocess can replay it.
        """

        rule = classify_review_issue(reason_code)
        if self._open_blocking_review_count(document.id) == 0:
            self._review(
                document=document,
                issue_code=reason_code,
                field_path=_field_path_for_reason(reason_code),
                observed={
                    "stage": stage.value,
                    "validation_policy_version": VALIDATION_POLICY_VERSION,
                },
            )
        ocr_provider, _, ocr_version = (document.ocr_provider or "").partition("/")
        snapshot = document.structured_snapshot or {}
        ledger = self.session.scalar(
            select(UnresolvedIngestion).where(UnresolvedIngestion.document_id == document.id)
        )
        if ledger is None:
            ledger = UnresolvedIngestion(document_id=document.id, retry_count=0)
            self.session.add(ledger)
        ledger.template = document.parser_template or UNKNOWN_TEMPLATE
        ledger.failure_stage = stage
        ledger.failure_reason_code = reason_code
        ledger.reason_category = rule.category
        ledger.critical_fields_affected = list(
            _CRITICAL_FIELDS_BY_REASON.get(reason_code, ("document.ingestion",))
        )
        ledger.parser_name = document.parser_template
        ledger.parser_version = snapshot.get("parser_version") or document.parser_version
        ledger.normalizer_version = self.settings.drug_dictionary_version
        ledger.validation_policy_version = VALIDATION_POLICY_VERSION
        ledger.ocr_provider = ocr_provider or None
        ledger.ocr_version = ocr_version or None
        ledger.retry_count += 1
        ledger.last_attempt_at = utcnow()
        ledger.resolved_at = None
        ledger.resolution = None

        document.failure_code = reason_code
        document.processing_payload = {}
        document.processing_status = ProcessingStatus.UNRESOLVED
        job.stage = ProcessingStatus.UNRESOLVED
        job.status = JobStatus.COMPLETED
        job.finished_at = utcnow()
        self._sync_import_item(document)
        self._update_batch(document)
        append_audit_event(
            self.session,
            action="pipeline.unresolved",
            entity_type="document",
            entity_id=document.id,
            after_redacted={
                "status": ProcessingStatus.UNRESOLVED.value,
                "reason_code": reason_code,
                "reason_category": rule.category,
                "template": ledger.template,
                "validation_policy_version": VALIDATION_POLICY_VERSION,
            },
        )
        self.session.commit()
        return PipelineResult(
            document.id,
            ProcessingStatus.UNRESOLVED,
            review_count=self._open_review_count(document.id),
        )

    def _mark_unresolved_resolved(self, document_id: uuid.UUID, resolution: str) -> None:
        ledger = self.session.scalar(
            select(UnresolvedIngestion).where(UnresolvedIngestion.document_id == document_id)
        )
        if ledger is not None and ledger.resolved_at is None:
            ledger.resolved_at = utcnow()
            ledger.resolution = resolution

    def _finish_existing(
        self, document: Document, job: ProcessingJob, encounter: Encounter
    ) -> PipelineResult:
        blocking = self._open_blocking_review_count(document.id)
        terminal = ProcessingStatus.UNRESOLVED if blocking else ProcessingStatus.ACCEPTED
        document.processing_status = terminal
        job.stage = terminal
        job.status = JobStatus.COMPLETED
        job.finished_at = utcnow()
        if terminal == ProcessingStatus.ACCEPTED:
            self._mark_unresolved_resolved(document.id, "auto_accepted")
        self._sync_import_item(document)
        self._update_batch(document)
        self.session.commit()
        return PipelineResult(
            document.id,
            terminal,
            patient_id=encounter.patient_id,
            encounter_id=encounter.id,
            review_count=self._open_review_count(document.id),
        )

    def _result_if_terminal(self, document: Document) -> PipelineResult | None:
        if document.processing_status not in TERMINAL_STATUSES:
            return None
        encounter = self.session.scalar(
            select(Encounter).where(Encounter.primary_document_id == document.id)
        )
        return PipelineResult(
            document.id,
            document.processing_status,
            patient_id=encounter.patient_id if encounter else None,
            encounter_id=encounter.id if encounter else None,
            review_count=self._open_review_count(document.id),
        )

    def _record_failure(self, document_id: uuid.UUID, code: str, *, retryable: bool) -> None:
        document = self.session.get(Document, document_id)
        job = self._get_job(document_id)
        if document is None or job is None:
            return
        document.failure_code = code
        if retryable and job.attempt < 4:
            document.processing_status = ProcessingStatus.QUEUED
            job.status = JobStatus.RETRY_SCHEDULED
            job.stage = ProcessingStatus.QUEUED
        else:
            document.processing_status = ProcessingStatus.FAILED
            job.status = JobStatus.FAILED
            job.stage = ProcessingStatus.FAILED
            job.finished_at = utcnow()
        job.error_code = code
        job.error_detail_redacted = "Provider or pipeline error; inspect protected job metadata"
        self._sync_import_item(document)
        self._update_batch(document)
        self.session.commit()

    def _get_job(self, document_id: uuid.UUID) -> ProcessingJob | None:
        key = f"{document_id}:{self.settings.pipeline_version}"
        return self.session.scalar(
            select(ProcessingJob).where(ProcessingJob.idempotency_key == key)
        )

    def _open_review_count(self, document_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count(ReviewItem.id)).where(
                    ReviewItem.document_id == document_id,
                    ReviewItem.status == ReviewStatus.OPEN,
                )
            )
            or 0
        )

    def _open_blocking_review_count(self, document_id: uuid.UUID) -> int:
        return int(
            self.session.scalar(
                select(func.count(ReviewItem.id)).where(
                    ReviewItem.document_id == document_id,
                    ReviewItem.status == ReviewStatus.OPEN,
                    ReviewItem.blocking.is_(True),
                )
            )
            or 0
        )

    def _open_blocking_reason(self, document_id: uuid.UUID) -> str:
        return str(
            self.session.scalar(
                select(ReviewItem.issue_code)
                .where(
                    ReviewItem.document_id == document_id,
                    ReviewItem.status == ReviewStatus.OPEN,
                    ReviewItem.blocking.is_(True),
                )
                .order_by(ReviewItem.created_at)
                .limit(1)
            )
            or "UNSUPPORTED_TEMPLATE"
        )

    def _sync_import_item(self, document: Document) -> None:
        items = self.session.scalars(
            select(ImportItem).where(ImportItem.document_id == document.id)
        ).all()
        for item in items:
            item.status = document.processing_status
            item.error_code = document.failure_code

    def _update_batch(self, document: Document) -> None:
        items = self.session.scalars(
            select(ImportItem).where(ImportItem.document_id == document.id)
        ).all()
        self.session.flush()
        for batch_id in {item.batch_id for item in items}:
            batch = self.session.get(ImportBatch, batch_id)
            if batch is None:
                continue
            statuses = list(
                self.session.scalars(
                    select(ImportItem.status).where(ImportItem.batch_id == batch.id)
                )
            )
            batch.total_items = len(statuses)
            batch.accepted_items = statuses.count(ProcessingStatus.ACCEPTED)
            batch.review_items = statuses.count(ProcessingStatus.REVIEW_REQUIRED)
            batch.failed_items = statuses.count(ProcessingStatus.FAILED)
            batch.unresolved_items = statuses.count(ProcessingStatus.UNRESOLVED)
            active = [
                status
                for status in statuses
                if status not in TERMINAL_STATUSES | {ProcessingStatus.FAILED}
            ]
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


def _issue(issue_code: str, field_path: str) -> dict:
    return {"issue_code": issue_code, "field_path": field_path}


def _field_path_for_reason(reason_code: str) -> str:
    fields = _CRITICAL_FIELDS_BY_REASON.get(reason_code, ("document.ingestion"))
    return fields[0]
