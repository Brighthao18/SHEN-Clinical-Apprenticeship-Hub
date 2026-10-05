"""Versioned ingestion decision policy.

Phase 5.2 split the legacy single `review_required` terminal into a
blocking-aware model: only issues that could actually pollute the clinical
database may block auto-accept; documents with any blocking issue terminate
as UNRESOLVED (no manual action expected).

Phase 6 keeps the same terminal model but tightens it for the AI-primary
architecture (MinerU -> ClinicalAIExtractor -> Validator -> Resolution):

- AI-extraction gate failures (schema rejection, low confidence,
  untraceable evidence, unavailable provider) are blocking.
- Missing encounter date/hospital block patient/encounter creation.
- Duplicated items and untraceable drug identity block
  (``DRUG_IDENTITY_UNVERIFIABLE``).
- ``HOSPITAL_SPECIFICATION_UNKNOWN`` stays non-blocking: the calculated dose
  stays NULL and is recomputable once the user maintains specifications.

Phase 6.1 splits the former blocking ``UNKNOWN_DRUG`` semantics: a raw drug
name that IS traceable to its cited OCR evidence but merely lacks a
canonical DrugDictionary mapping no longer blocks the encounter — the item
is stored with ``normalization_status='pending'`` (canonical NULL, AI
suggestion recorded, never authoritative). Blocking is reserved for cases
where raw identity itself is unproven (untraceable/garbled/non-drug).

The policy table is versioned; every decision output records
`validation_policy_version` so benchmark results stay reproducible.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

VALIDATION_POLICY_VERSION = "6.1.0"

DECISION_AUTO_ACCEPT = "auto_accept"
DECISION_UNRESOLVED = "unresolved"

REASON_CATEGORIES = (
    "CRITICAL_CLINICAL_UNCERTAINTY",
    "NON_CRITICAL_RAW_RESIDUE",
    "REFERENCE_DATA_LIMITATION",
    "UNSUPPORTED_TEMPLATE",
    "OCR_INCOMPLETENESS",
    "OTHER",
)

CRITICALITY_CRITICAL = "critical"
CRITICALITY_NON_CRITICAL = "non_critical"


@dataclass(frozen=True)
class ReviewReasonRule:
    issue_code: str
    category: str
    criticality: str
    blocking: bool
    reason: str


# Blocking rules: the recorded value could be wrong in a way that pollutes a
# critical clinical field, or a critical field cannot be trusted yet.
_BLOCKING_RULES: tuple[ReviewReasonRule, ...] = (
    ReviewReasonRule(
        "PATIENT_NAME_MISSING",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "Patient identity cannot be established; wrong-patient data risk.",
    ),
    ReviewReasonRule(
        "MEDICAL_RECORD_NUMBER_MISSING",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "No stable hospital+MRN fingerprint; patient matching is unsafe.",
    ),
    ReviewReasonRule(
        "PATIENT_MATCH_REVIEW_REQUIRED",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "Ambiguous patient match; V1 never auto-merges fuzzy candidates.",
    ),
    ReviewReasonRule(
        "PATIENT_IDENTITY_NAME_CONFLICT",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "Hospital+MRN fingerprint matched but the extracted name conflicts.",
    ),
    ReviewReasonRule(
        "PATIENT_IDENTITY_SEX_CONFLICT",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "Hospital+MRN fingerprint matched but sex conflicts.",
    ),
    ReviewReasonRule(
        "MULTIPLE_PATIENT_NAMES_SUSPECTED",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "More than one distinct patient name in one source document.",
    ),
    ReviewReasonRule(
        "ENCOUNTER_DATE_MISSING",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "No encounter date could be extracted; timeline ordering is unsafe.",
    ),
    ReviewReasonRule(
        "HOSPITAL_MISSING",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "No hospital could be established; the hospital+MRN patient "
        "fingerprint cannot be built.",
    ),
    ReviewReasonRule(
        "AI_EXTRACTION_PROVIDER_FAILED",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "The clinical AI extraction provider is unavailable or has no "
        "trustworthy result for this document.",
    ),
    ReviewReasonRule(
        "AI_EXTRACTION_SCHEMA_REJECTED",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "Clinical AI extraction output failed the strict structured schema.",
    ),
    ReviewReasonRule(
        "AI_EXTRACTION_LOW_CONFIDENCE",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "Clinical AI extraction confidence is below the configured floor; "
        "confidence is never the sole accept basis.",
    ),
    ReviewReasonRule(
        "AI_EXTRACTION_EVIDENCE_MISSING",
        "OCR_INCOMPLETENESS",
        CRITICALITY_CRITICAL,
        True,
        "An extracted prescription item cannot be traced back to the stored "
        "OCR text; the item may be fabricated.",
    ),
    ReviewReasonRule(
        "DUPLICATED_PRESCRIPTION_ITEM",
        "OCR_INCOMPLETENESS",
        CRITICALITY_CRITICAL,
        True,
        "The same drug/processing/dose appears more than once; item "
        "completeness and counts are unproven.",
    ),
    ReviewReasonRule(
        "UNLABELED_HEADER_MRN_INFERRED",
        "OCR_INCOMPLETENESS",
        CRITICALITY_CRITICAL,
        True,
        (
            "MRN inferred from an unlabeled header number; a wrong inference "
            "risks wrong patient creation or merge (pending OD-015 blinded "
            "layout-anchor validation)."
        ),
    ),
    ReviewReasonRule(
        "PACKAGE_UNIT_MISSING_IN_OCR",
        "OCR_INCOMPLETENESS",
        CRITICALITY_CRITICAL,
        True,
        "Package count without a 包/袋 unit; the count semantics and the "
        "calculated dose cannot be verified.",
    ),
    ReviewReasonRule(
        "UNPARSED_PRESCRIPTION_RESIDUAL",
        "OCR_INCOMPLETENESS",
        CRITICALITY_CRITICAL,
        True,
        "Prescription-zone residue may contain unparsed drug items "
        "(CASE004 failure mode); item completeness is unproven.",
    ),
    ReviewReasonRule(
        "LEGACY_UNPARSED_RESIDUAL",
        "OCR_INCOMPLETENESS",
        CRITICALITY_CRITICAL,
        True,
        "Legacy prescription lines could not be fully consumed; item completeness is unproven.",
    ),
    ReviewReasonRule(
        "PRESCRIPTION_NOT_FOUND",
        "UNSUPPORTED_TEMPLATE",
        CRITICALITY_CRITICAL,
        True,
        "No prescription items could be extracted from the source.",
    ),
    ReviewReasonRule(
        "JNU_PARSER_INCOMPLETE",
        "UNSUPPORTED_TEMPLATE",
        CRITICALITY_CRITICAL,
        True,
        "Current-template parsing failed; required fields are missing.",
    ),
    ReviewReasonRule(
        "LEGACY_PARSER_INCOMPLETE",
        "UNSUPPORTED_TEMPLATE",
        CRITICALITY_CRITICAL,
        True,
        "Legacy-template parsing failed; required fields are missing.",
    ),
    ReviewReasonRule(
        "GENERIC_PARSER_REVIEW_REQUIRED",
        "UNSUPPORTED_TEMPLATE",
        CRITICALITY_CRITICAL,
        True,
        "No supported template detected for this document.",
    ),
    ReviewReasonRule(
        "UNSUPPORTED_TEMPLATE",
        "UNSUPPORTED_TEMPLATE",
        CRITICALITY_CRITICAL,
        True,
        "Template is not supported by any production parser.",
    ),
    ReviewReasonRule(
        "LLM_STRUCTURING_SCHEMA_REJECTED",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "LLM-assisted structuring output failed strict schema validation.",
    ),
    ReviewReasonRule(
        "LLM_STRUCTURING_LOW_CONFIDENCE",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "LLM-assisted structuring confidence is below the auto-accept floor; "
        "LLM confidence is never the sole accept basis.",
    ),
    ReviewReasonRule(
        "DRUG_IDENTITY_UNVERIFIABLE",
        "CRITICAL_CLINICAL_UNCERTAINTY",
        CRITICALITY_CRITICAL,
        True,
        "The raw drug name is not traceable to its cited OCR evidence, or is "
        "suspected garbled/non-drug content; identity itself is unproven and "
        "the item must not be stored.",
    ),
)

# Non-blocking rules: the stored fact is faithful to the source, stays NULL
# where unknown, and is recomputable once user-maintained reference data or a
# parser upgrade exists. They are recorded as warnings, never as fabricated
# clinical values.
_NON_BLOCKING_RULES: tuple[ReviewReasonRule, ...] = (
    ReviewReasonRule(
        "UNKNOWN_DRUG",
        "REFERENCE_DATA_LIMITATION",
        CRITICALITY_NON_CRITICAL,
        False,
        "Phase 6.1: the raw name is traceable to OCR evidence but the "
        "DrugDictionary lacks a canonical mapping. The item is stored with "
        "normalization_status='pending' (canonical NULL, AI suggestion "
        "recorded, never authoritative); recomputable after dictionary growth.",
    ),
    ReviewReasonRule(
        "HOSPITAL_SPECIFICATION_UNKNOWN",
        "REFERENCE_DATA_LIMITATION",
        CRITICALITY_NON_CRITICAL,
        False,
        "Calculated dose stays NULL; recomputable after the user enters "
        "manual_verified hospital specifications.",
    ),
    ReviewReasonRule(
        "SEX_UNKNOWN",
        "NON_CRITICAL_RAW_RESIDUE",
        CRITICALITY_NON_CRITICAL,
        False,
        "Sex is not a critical benchmark field; stored as unknown.",
    ),
    ReviewReasonRule(
        "AGE_OUT_OF_RANGE",
        "NON_CRITICAL_RAW_RESIDUE",
        CRITICALITY_NON_CRITICAL,
        False,
        "Encounter-scoped age is discarded; not a critical field.",
    ),
    ReviewReasonRule(
        "LEGACY_ADMINISTRATIVE_FIELD_SKIPPED",
        "NON_CRITICAL_RAW_RESIDUE",
        CRITICALITY_NON_CRITICAL,
        False,
        "Legacy administrative field intentionally ignored by the research-focused legacy parser.",
    ),
)

REVIEW_REASON_POLICY: dict[str, ReviewReasonRule] = {
    rule.issue_code: rule for rule in (*_BLOCKING_RULES, *_NON_BLOCKING_RULES)
}

_FAIL_CLOSED_RULE = ReviewReasonRule(
    "__UNKNOWN_ISSUE_CODE__",
    "OTHER",
    CRITICALITY_CRITICAL,
    True,
    "Issue code is not in the versioned policy table; fail closed.",
)


def classify_review_issue(issue_code: str) -> ReviewReasonRule:
    """Classify a review issue; unknown codes fail closed as blocking."""

    rule = REVIEW_REASON_POLICY.get(issue_code)
    if rule is None:
        return ReviewReasonRule(
            issue_code=issue_code,
            category=_FAIL_CLOSED_RULE.category,
            criticality=_FAIL_CLOSED_RULE.criticality,
            blocking=_FAIL_CLOSED_RULE.blocking,
            reason=_FAIL_CLOSED_RULE.reason,
        )
    return rule


@dataclass(frozen=True)
class IngestionDecision:
    decision: str
    blocking_reasons: tuple[str, ...]
    non_blocking_warnings: tuple[str, ...]
    validation_policy_version: str


def decide_ingestion(issue_codes: Iterable[str]) -> IngestionDecision:
    """Map review issue codes to the Phase 5.2 terminal decision.

    Any blocking issue routes the document to UNRESOLVED. Reference-data
    limitations and non-critical residue never block auto-accept, but every
    warning is still recorded and recomputable.
    """

    blocking: list[str] = []
    non_blocking: list[str] = []
    for issue_code in dict.fromkeys(issue_codes):
        rule = classify_review_issue(issue_code)
        (blocking if rule.blocking else non_blocking).append(rule.issue_code)
    decision = DECISION_UNRESOLVED if blocking else DECISION_AUTO_ACCEPT
    return IngestionDecision(
        decision=decision,
        blocking_reasons=tuple(sorted(blocking)),
        non_blocking_warnings=tuple(sorted(non_blocking)),
        validation_policy_version=VALIDATION_POLICY_VERSION,
    )


def analyze_review_reasons(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-issue review-cause analysis for benchmark and drilldown reports.

    Input issues follow the calibration validation shape
    (``{"issue_code": ..., "field_path": ...}``); output never includes
    clinical values.
    """

    analysis: list[dict[str, Any]] = []
    for issue in issues:
        issue_code = str(issue.get("issue_code", ""))
        rule = classify_review_issue(issue_code)
        analysis.append(
            {
                "review_rule_id": rule.issue_code,
                "trigger_field": issue.get("field_path"),
                "reason_category": rule.category,
                "criticality": rule.criticality,
                "blocking": rule.blocking,
                "reason": rule.reason,
            }
        )
    return analysis
