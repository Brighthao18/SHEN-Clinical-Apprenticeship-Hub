from enum import StrEnum


class Sex(StrEnum):
    MALE = "male"
    FEMALE = "female"
    OTHER = "other"
    UNKNOWN = "unknown"


class SourceType(StrEnum):
    PHYSICIAN_RECORD = "physician_record"
    STUDENT_NOTE = "student_note"
    AI_NORMALIZED = "ai_normalized"
    AI_INFERRED = "ai_inferred"
    EXTERNAL_REFERENCE = "external_reference"


class DocumentType(StrEnum):
    OUTPATIENT_RECORD = "outpatient_record"
    PRESCRIPTION_SLIP = "prescription_slip"
    LAB_REPORT = "lab_report"
    TONGUE_IMAGE = "tongue_image"
    OTHER = "other"


class ProcessingStatus(StrEnum):
    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    OCR_RUNNING = "OCR_RUNNING"
    OCR_COMPLETED = "OCR_COMPLETED"
    TEMPLATE_DETECTED = "TEMPLATE_DETECTED"
    PARSING = "PARSING"
    STRUCTURING = "STRUCTURING"
    PATIENT_MATCHING = "PATIENT_MATCHING"
    NORMALIZING = "NORMALIZING"
    VALIDATING = "VALIDATING"
    # Legacy terminal kept for pre-5.2 rows and the optional interactive
    # review workflow; Phase 5.2 pipelines no longer emit it.
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    ACCEPTED = "ACCEPTED"
    # Phase 5.2: safe-fail terminal. No manual action is expected; the
    # document stays out of the committed clinical dataset until an explicit
    # reprocess with a newer parser safely accepts it.
    UNRESOLVED = "UNRESOLVED"
    FAILED = "FAILED"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRY_SCHEDULED = "retry_scheduled"


class ReviewStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class SymptomStatus(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class SymptomTrend(StrEnum):
    NEW = "new"
    PERSISTENT = "persistent"
    IMPROVED = "improved"
    WORSENED = "worsened"
    RESOLVED = "resolved"
    UNKNOWN = "unknown"


class DiagnosisType(StrEnum):
    WESTERN = "western"
    TCM_DISEASE = "tcm_disease"
    TCM_PATTERN = "tcm_pattern"
    PROCEDURE_STATUS = "procedure_status"
    PROBLEM = "problem"


class DoseSource(StrEnum):
    EXPLICIT = "explicit"
    HOSPITAL_SPEC = "hospital_spec"
    CALCULATED = "calculated"
    UNKNOWN = "unknown"


class DoseScope(StrEnum):
    PER_DECOCTION = "per_decoction"
    TOTAL_PRESCRIPTION = "total_prescription"
    UNKNOWN = "unknown"


class AITaskType(StrEnum):
    ENCOUNTER_INTERPRETATION = "encounter_interpretation"
    PATIENT_LONGITUDINAL_ANALYSIS = "patient_longitudinal_analysis"
    PRESCRIPTION_DIFF_ANALYSIS = "prescription_diff_analysis"
    FORMULA_IDENTIFICATION = "formula_identification"
    SIMILAR_CASE_ANALYSIS = "similar_case_analysis"
    TEACHER_PATTERN_ANALYSIS = "teacher_pattern_analysis"
    STUDY_NOTE_GENERATION = "study_note_generation"
    FREE_RESEARCH = "free_research"
    # Phase 9 apprenticeship writing prompt-template keys.
    TEACHER_NOTE_GENERATION = "teacher_note_generation"
    CASE_RECORD_GENERATION = "case_record_generation"


class ApprenticeshipNoteType(StrEnum):
    """Phase 9 apprenticeship writing document types."""

    TEACHER_NOTE = "teacher_note"
    CASE_RECORD = "case_record"


class NoteVersionType(StrEnum):
    """Authorship layer of one apprenticeship note version.

    ``user_original`` is the user's own first-hand text, ``ai_draft`` is an
    AI-generated draft, ``user_draft``/``user_final`` are user-authored saves
    (edits of any earlier version). All layers are permanently retained;
    rows are append-only.
    """

    USER_ORIGINAL = "user_original"
    AI_DRAFT = "ai_draft"
    USER_DRAFT = "user_draft"
    USER_FINAL = "user_final"


class NoteKind(StrEnum):
    TONGUE = "tongue"
    PULSE = "pulse"
    TEACHER_ORAL = "teacher_oral"
    STUDENT_OBSERVATION = "student_observation"
    STUDENT_INTERPRETATION = "student_interpretation"
