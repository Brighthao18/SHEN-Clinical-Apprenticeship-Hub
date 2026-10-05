from __future__ import annotations

import hmac
import json
import re
import unicodedata
import uuid
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.models.enums import Sex
from app.services.drugs import PROCESSING_NORMALIZATION, SEED_DRUGS
from app.services.parsers.types import ParsedDocument
from app.services.validation_policy import (
    VALIDATION_POLICY_VERSION,
    analyze_review_reasons,
    decide_ingestion,
)

ERROR_TAXONOMY = {
    "OCR_ERROR",
    "TEMPLATE_PARSER_ERROR",
    "NORMALIZATION_ERROR",
    "REFERENCE_DATA_MISSING",
    "PATIENT_MATCH_ERROR",
    "VALIDATION_POLICY_ERROR",
    "GOLD_ANNOTATION_ERROR",
    "OTHER",
}
FIRST_FIVE_CASE_IDS = ("CASE001", "CASE002", "CASE003", "CASE004", "CASE005")
PRODUCTION_GATE_MINIMUM_REPRESENTATIVE_CASES = 20
CURRENT_PRODUCTION_GATE_MINIMUM_AUTO_ACCEPTED_CASES = 10
SUPPORTED_CASE_TYPES = {"JNU_CURRENT_V1", "JNU_LEGACY_V1", "UNKNOWN"}
SUPPORTED_EXPECTED_DECISIONS = {
    "AUTO_ACCEPT",
    "AUTO_ACCEPT_IF_SAFE",
    "UNRESOLVED",
}

METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "patient_match_accuracy": {
        "numerator": "cases with correct patient-match decision and target",
        "denominator": "cases whose Gold records a patient-match expectation",
        "null_handling": "Gold without a match expectation is excluded",
    },
    "encounter_date_accuracy": {
        "numerator": "cases with the exact Gold encounter date",
        "denominator": "cases with a non-null Gold encounter date",
        "null_handling": "Gold NULL/not-recorded dates are excluded",
    },
    "prescription_item_recall": {
        "numerator": "Gold items with a unique semantic alignment",
        "denominator": "all Gold prescription items",
        "null_handling": "an absent Gold item list contributes no denominator",
    },
    "prescription_item_precision": {
        "numerator": "actual items with a unique semantic alignment",
        "denominator": "all actual prescription items",
        "null_handling": "an absent actual item list contributes no denominator",
    },
    "drug_identity_accuracy": {
        "numerator": "aligned items with exact canonical drug identity",
        "denominator": "aligned items with non-null Gold canonical identity",
        "null_handling": "Gold NULL/not-recorded identity is excluded",
    },
    "processing_accuracy": {
        "numerator": "aligned items with exact processing method",
        "denominator": "aligned items with a non-null Gold processing method",
        "null_handling": "Gold NULL/not-recorded processing is excluded",
    },
    "explicit_dose_accuracy": {
        "numerator": "aligned items with exact explicit package dose",
        "denominator": "aligned items with a non-null Gold explicit package dose",
        "null_handling": "Gold NULL/not-recorded dose is excluded",
    },
    "package_count_accuracy": {
        "numerator": "aligned items with exact package count",
        "denominator": "aligned items with a non-null Gold package count",
        "null_handling": "Gold NULL/not-recorded count is excluded",
    },
    "derived_dose_accuracy": {
        "numerator": "aligned items with exact calculated per-decoction dose",
        "denominator": "aligned items with a non-null Gold calculated dose",
        "null_handling": "Gold NULL/not-recorded derived dose is excluded",
    },
    "unresolved_recall": {
        "numerator": "expected-unresolved cases routed to unresolved",
        "denominator": "all cases expected to be unresolved",
        "null_handling": "cases without an expected ingestion decision are excluded",
    },
    "auto_accept_precision": {
        "numerator": "auto-accepted cases with every critical field correct",
        "denominator": "all auto-accepted cases",
        "null_handling": "non-auto-accepted cases are excluded",
    },
    "critical_false_auto_accept": {
        "numerator": "auto-accepted cases with at least one critical error",
        "denominator": "all benchmark cases",
        "null_handling": "incomplete benchmark cases are excluded",
    },
}


@dataclass(frozen=True)
class GoldenCaseReference:
    case_id: str
    document_id: uuid.UUID
    image_path: Path
    gold_path: Path
    case_role: str | None
    expected_match_case_id: str | None
    case_type: str | None
    expected_ingestion_decision: str | None
    test_intents: tuple[str, ...]


@dataclass(frozen=True)
class GoldenCase(GoldenCaseReference):
    expected: dict[str, Any]
    annotation_status: str


class PatientMatchTracker:
    """Case-scoped exact-MRN projection; values never enter redacted reports."""

    def __init__(self) -> None:
        self._first_case_by_mrn: dict[str, str] = {}

    def evaluate(self, case_id: str, medical_record_number: str | None) -> dict[str, Any]:
        if not medical_record_number:
            return {"decision": "review_required", "matched_case_id": None}
        normalized_mrn = unicodedata.normalize("NFKC", medical_record_number).strip().casefold()
        first_case = self._first_case_by_mrn.get(normalized_mrn)
        if first_case is None:
            self._first_case_by_mrn[normalized_mrn] = case_id
            return {"decision": "new_patient", "matched_case_id": None}
        return {"decision": "exact_mrn_match", "matched_case_id": first_case}


def load_selected_case_references(
    manifest_path: Path,
    dataset_root: Path,
    selected_case_ids: list[str],
    *,
    require_image: bool = True,
) -> list[GoldenCaseReference]:
    root = dataset_root.resolve(strict=True)
    manifest = _safe_file(root, manifest_path.resolve(strict=True))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if payload.get("schema_version") not in {"1.0", "2.0", "3.0"} or not isinstance(
        payload.get("cases"), list
    ):
        raise ValueError("Golden manifest must use schema_version 1.0/2.0 and a cases list")
    _validate_explicit_case_selection(selected_case_ids)

    by_id: dict[str, dict[str, Any]] = {}
    document_ids: set[uuid.UUID] = set()
    for raw_case in payload["cases"]:
        if not isinstance(raw_case, dict):
            raise ValueError("Every manifest case must be an object")
        case_id = raw_case.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in by_id:
            raise ValueError("Manifest case IDs must be non-empty and unique")
        document_id = uuid.UUID(str(raw_case.get("document_id")))
        if document_id in document_ids:
            raise ValueError("Manifest document IDs must be unique")
        document_ids.add(document_id)
        by_id[case_id] = raw_case

    missing = [case_id for case_id in selected_case_ids if case_id not in by_id]
    if missing:
        raise ValueError("One or more selected case IDs are absent from the manifest")

    selected: list[GoldenCaseReference] = []
    for case_id in selected_case_ids:
        raw_case = by_id[case_id]
        case_type = _optional_string(raw_case.get("case_type"))
        if case_type is not None and case_type not in SUPPORTED_CASE_TYPES:
            raise ValueError("Manifest case_type is unsupported")
        expected_decision = _optional_string(raw_case.get("expected_ingestion_decision"))
        if expected_decision is not None and expected_decision not in SUPPORTED_EXPECTED_DECISIONS:
            raise ValueError("Manifest expected_ingestion_decision is unsupported")
        image = _resolve_relative_path(root, raw_case.get("image"), must_exist=require_image)
        gold = _resolve_relative_path(root, raw_case.get("gold"), must_exist=False)
        if gold.name != f"{case_id}.json":
            raise ValueError("Each Gold path must be gold/<CASE_ID>.json")
        selected.append(
            GoldenCaseReference(
                case_id=case_id,
                document_id=uuid.UUID(str(raw_case["document_id"])),
                image_path=image,
                gold_path=gold,
                case_role=_optional_string(raw_case.get("case_role")),
                expected_match_case_id=_optional_string(raw_case.get("expected_match_case_id")),
                case_type=case_type,
                expected_ingestion_decision=expected_decision,
                test_intents=_string_tuple(raw_case.get("test_intents", [])),
            )
        )
    return selected


def load_selected_golden_cases(
    manifest_path: Path,
    dataset_root: Path,
    selected_case_ids: list[str],
    *,
    require_confirmed: bool = False,
) -> list[GoldenCase]:
    references = load_selected_case_references(
        manifest_path,
        dataset_root,
        selected_case_ids,
        require_image=True,
    )
    selected: list[GoldenCase] = []
    root = dataset_root.resolve(strict=True)
    for reference in references:
        gold = _safe_file(root, reference.gold_path.resolve(strict=True))
        gold_payload = json.loads(gold.read_text(encoding="utf-8"))
        expected = gold_payload.get("expected")
        if gold_payload.get("schema_version") not in {"1.0", "2.0", "3.0"} or not isinstance(
            expected, dict
        ):
            raise ValueError("Gold JSON must use schema_version 1.0/2.0 and an expected object")
        annotation = gold_payload.get("annotation", {})
        status = annotation.get("status", "legacy") if isinstance(annotation, dict) else "legacy"
        if require_confirmed and status != "confirmed":
            raise ValueError(f"Gold annotation for {reference.case_id} is not confirmed")
        selected.append(
            GoldenCase(
                **reference.__dict__,
                expected=expected,
                annotation_status=str(status),
            )
        )
    return selected


def validate_first_five_manifest(manifest_path: Path, dataset_root: Path) -> dict[str, Any]:
    references = load_selected_case_references(
        manifest_path,
        dataset_root,
        list(FIRST_FIVE_CASE_IDS),
        require_image=False,
    )
    if tuple(reference.case_id for reference in references) != FIRST_FIVE_CASE_IDS:
        raise ValueError("First-five manifest must contain CASE001 through CASE005 in order")
    expected_match = {
        reference.case_id: reference.expected_match_case_id for reference in references
    }
    if expected_match["CASE002"] is not None or expected_match["CASE003"] != "CASE002":
        raise ValueError("CASE003 must expect an exact match to CASE002")
    return {
        "status": "first_five_manifest_ready",
        "case_count": len(references),
        "case_ids": list(FIRST_FIVE_CASE_IDS),
        "images_present": sum(reference.image_path.is_file() for reference in references),
        "gold_present": sum(reference.gold_path.is_file() for reference in references),
        "automatic_directory_traversal": False,
        "production_gate_evaluable": False,
    }


def build_assisted_gold_draft(
    reference: GoldenCaseReference,
    expected_candidate: dict[str, Any],
    *,
    run_id: str,
    candidate_snapshot_reference: str,
    candidate_snapshot_sha256: str,
    created_at: str,
) -> dict[str, Any]:
    """Build a human-review draft without treating provider output as Gold."""
    if not isinstance(expected_candidate, dict):
        raise TypeError("Assisted annotation candidate must be an object")
    if not run_id or not candidate_snapshot_reference:
        raise ValueError("Assisted annotation requires a frozen run and snapshot reference")
    if not re.fullmatch(r"[0-9a-f]{64}", candidate_snapshot_sha256):
        raise ValueError("Candidate snapshot SHA-256 must be lowercase hexadecimal")
    return {
        "schema_version": "2.0",
        "case_id": reference.case_id,
        "document_id": str(reference.document_id),
        "case_type": reference.case_type,
        "expected_ingestion_decision": reference.expected_ingestion_decision,
        "test_intents": list(reference.test_intents),
        "annotation": {
            "status": "draft",
            "updated_at": created_at,
            "confirmed_at": None,
            "tool": "local_golden_annotation_v1",
            "entry_mode": "mineru_assisted_correction",
            "candidate_frozen": True,
            "candidate_run_id": run_id,
            "candidate_snapshot_reference": candidate_snapshot_reference,
            "candidate_snapshot_sha256": candidate_snapshot_sha256,
            "requires_full_human_verification": True,
        },
        "expected": expected_candidate,
    }


def verify_assisted_candidate_snapshot(
    annotation: Any,
    *,
    run_id: str,
    actual_sha256: str,
) -> None:
    """Fail closed if an assisted Gold draft no longer points at its frozen candidate."""
    if not isinstance(annotation, dict):
        return
    if annotation.get("entry_mode") != "mineru_assisted_correction":
        return
    if annotation.get("candidate_frozen") is not True:
        raise ValueError("Assisted candidate is not marked frozen")
    if annotation.get("candidate_run_id") != run_id:
        raise ValueError("Assisted candidate belongs to a different run")
    if not isinstance(annotation.get("candidate_snapshot_reference"), str):
        raise ValueError("Assisted candidate reference is missing")
    expected_sha256 = annotation.get("candidate_snapshot_sha256")
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("Assisted candidate SHA-256 is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", actual_sha256):
        raise ValueError("Actual candidate SHA-256 is invalid")
    if not hmac.compare_digest(expected_sha256, actual_sha256):
        raise ValueError("Frozen candidate SHA-256 mismatch")


def normalize_ocr_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.splitlines()).strip() + "\n"


def build_normalized_clinical_result(
    parsed: ParsedDocument,
    *,
    patient_match: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    items: list[dict[str, Any]] = []
    issues: list[dict[str, str]] = []
    for index, parsed_item in enumerate(parsed.prescription_items):
        seed = SEED_DRUGS.get(parsed_item.raw_drug_name.strip())
        canonical_name = seed[0] if seed else None
        processing = parsed_item.processing_method or (seed[1] if seed else None)
        if processing:
            processing = PROCESSING_NORMALIZATION.get(processing, processing)
        calculated = parsed_item.calculated_explicit_dose_g
        item = {
            "line_number": index + 1,
            "is_basic_drug": parsed_item.is_basic_drug,
            "raw_drug_name": parsed_item.raw_drug_name,
            "canonical_drug_name": canonical_name,
            "processing_method": processing,
            "explicit_package_dose_g": _decimal_string(parsed_item.explicit_package_dose_g),
            "package_count": _decimal_string(parsed_item.package_count),
            "calculated_dose_g": _decimal_string(calculated),
            "dose_scope": "per_decoction",
        }
        items.append(item)
        if canonical_name is None:
            issues.append(
                _issue("UNKNOWN_DRUG", f"prescription.items[{index}].canonical_drug_name")
            )
        if parsed_item.package_count is not None and parsed_item.original_package_unit is None:
            issues.append(
                _issue(
                    "PACKAGE_UNIT_MISSING_IN_OCR",
                    f"prescription.items[{index}].package_count",
                )
            )
        if parsed_item.explicit_package_dose_g is None and parsed_item.package_count is not None:
            issues.append(
                _issue(
                    "HOSPITAL_SPECIFICATION_UNKNOWN",
                    f"prescription.items[{index}].calculated_dose_g",
                )
            )

    if parsed.real_name is None:
        issues.append(_issue("PATIENT_NAME_MISSING", "patient_identity.real_name"))
    if parsed.medical_record_number is None:
        issues.append(
            _issue("MEDICAL_RECORD_NUMBER_MISSING", "patient_identity.medical_record_number")
        )
    if "UNLABELED_HEADER_MRN_INFERRED" in parsed.warnings:
        issues.append(
            _issue("UNLABELED_HEADER_MRN_INFERRED", "patient_identity.medical_record_number")
        )
    if parsed.sex == Sex.UNKNOWN:
        issues.append(_issue("SEX_UNKNOWN", "patient.sex"))
    if parsed.age is not None and not 0 <= parsed.age <= 130:
        issues.append(_issue("AGE_OUT_OF_RANGE", "encounter.age"))
    if not parsed.prescription_items:
        issues.append(_issue("PRESCRIPTION_NOT_FOUND", "prescription.item_count"))
    if "MULTIPLE_PATIENT_NAMES_SUSPECTED" in parsed.warnings:
        issues.append(_issue("MULTIPLE_PATIENT_NAMES_SUSPECTED", "patient_identity.real_name"))
    if has_suspicious_prescription_residual(parsed.raw_instruction_text):
        issues.append(_issue("UNPARSED_PRESCRIPTION_RESIDUAL", "prescription.unparsed_residual"))
    if patient_match["decision"] == "review_required":
        issues.append(_issue("PATIENT_MATCH_REVIEW_REQUIRED", "patient_match.decision"))

    review_flags = sorted({issue["issue_code"] for issue in issues})
    decision = decide_ingestion(review_flags)
    review_reasons = analyze_review_reasons(issues)
    normalized_result = {
        "patient_identity": {
            "real_name": parsed.real_name,
            "medical_record_number": parsed.medical_record_number,
        },
        "patient": {"sex": parsed.sex.value},
        "encounter": {
            "age": parsed.age,
            "encounter_date": (
                parsed.encounter_date.isoformat() if parsed.encounter_date else None
            ),
            "chief_complaint": parsed.chief_complaint,
            "past_history": parsed.past_history,
            "diagnosis": [diagnosis.raw for diagnosis in parsed.diagnoses],
            "physician": parsed.physician,
        },
        "prescription": {
            "item_count": len(items),
            "total_decoctions": parsed.total_decoctions,
            "items": items,
        },
        "patient_match": patient_match,
        "validation": {
            "review_flags": review_flags,
            "decision": decision.decision,
            "blocking_review_reasons": list(decision.blocking_reasons),
            "non_blocking_warnings": list(decision.non_blocking_warnings),
            "validation_policy_version": VALIDATION_POLICY_VERSION,
        },
    }
    validation_result = {
        "status": decision.decision,
        "issues": issues,
        "review_reasons": review_reasons,
        "review_flags": review_flags,
        "critical_checks_fail_closed": True,
        "validation_policy_version": VALIDATION_POLICY_VERSION,
    }
    decision_result = {
        "decision": decision.decision,
        "review_issue_count": len(issues),
        "blocking_review_reasons": list(decision.blocking_reasons),
        "non_blocking_warnings": list(decision.non_blocking_warnings),
        "auto_accept_threshold_relaxed": False,
        "llm_confidence_sole_accept_basis": False,
        "validation_policy_version": VALIDATION_POLICY_VERSION,
    }
    return normalized_result, validation_result, decision_result


def compare_expected_fields(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    expected_paths = _flatten(expected)
    actual_paths = _flatten(actual)
    matched: list[str] = []
    mismatched: list[str] = []
    for path, expected_value in sorted(expected_paths.items()):
        if path in actual_paths and actual_paths[path] == expected_value:
            matched.append(path)
        else:
            mismatched.append(path)
    return {
        "compared_fields": len(expected_paths),
        "matched_fields": len(matched),
        "mismatched_fields": len(mismatched),
        "matched_paths": matched,
        "mismatched_paths": mismatched,
    }


def compare_clinical_benchmark(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    case_type: str | None = None,
    expected_ingestion_decision: str | None = None,
) -> dict[str, Any]:
    expected_copy = deepcopy(expected)
    actual_copy = deepcopy(actual)
    expected_items = _prescription_items(expected_copy)
    actual_items = _prescription_items(actual_copy)
    alignment = align_prescription_items(expected_items, actual_items)
    _remove_prescription_items(expected_copy)
    _remove_prescription_items(actual_copy)
    expected_paths = _flatten(expected_copy)
    actual_paths = _flatten(actual_copy)
    matched: list[str] = []
    mismatches: list[dict[str, Any]] = []
    for path, expected_value in sorted(expected_paths.items()):
        if path in actual_paths and actual_paths[path] == expected_value:
            matched.append(path)
            continue
        mismatches.append(_mismatch(path, expected_paths, actual_paths))

    for expected_index, actual_index in alignment["matched_pairs"]:
        expected_item_paths = _flatten(
            expected_items[expected_index], f"prescription.items[{expected_index}]"
        )
        actual_item_paths = _flatten(
            actual_items[actual_index], f"prescription.items[{expected_index}]"
        )
        expected_paths.update(expected_item_paths)
        actual_paths.update(actual_item_paths)
        for path, expected_value in sorted(expected_item_paths.items()):
            if path in actual_item_paths and actual_item_paths[path] == expected_value:
                matched.append(path)
            else:
                mismatches.append(_mismatch(path, expected_paths, actual_paths))

    for expected_index in alignment["missing_expected_indices"]:
        path = f"prescription.items[{expected_index}]"
        mismatches.append(
            {
                "field_path": path,
                "layer": "CLINICAL_NORMALIZATION",
                "critical": True,
                "error_category": "TEMPLATE_PARSER_ERROR",
                "classification_source": "semantic_item_alignment",
            }
        )
    for actual_index in alignment["unexpected_actual_indices"]:
        mismatches.append(
            {
                "field_path": f"prescription.unexpected_items[{actual_index}]",
                "layer": "CLINICAL_NORMALIZATION",
                "critical": True,
                "error_category": "TEMPLATE_PARSER_ERROR",
                "classification_source": "semantic_item_alignment",
            }
        )
    if alignment["ambiguous"]:
        mismatches.append(
            {
                "field_path": "prescription.item_alignment",
                "layer": "SAFETY_DECISION",
                "critical": True,
                "error_category": "VALIDATION_POLICY_ERROR",
                "classification_source": "AMBIGUOUS_ITEM_MATCH",
            }
        )

    decision = actual.get("validation", {}).get("decision", "UNRESOLVED")
    critical_paths = [entry["field_path"] for entry in mismatches if entry["critical"]]
    false_auto_accept = _is_auto_accept(decision) and bool(critical_paths)
    layers: dict[str, dict[str, Any]] = {}
    for layer in ("SOURCE_EXTRACTION", "CLINICAL_NORMALIZATION", "SAFETY_DECISION"):
        layer_mismatches = [entry for entry in mismatches if entry["layer"] == layer]
        layer_matched = [path for path in matched if benchmark_layer_for_path(path) == layer]
        layers[layer] = {
            "matched_fields": len(layer_matched),
            "mismatched_fields": len(layer_mismatches),
            "critical_mismatched_fields": sum(entry["critical"] for entry in layer_mismatches),
            "mismatched_paths": [entry["field_path"] for entry in layer_mismatches],
        }
    metric_counts = _case_metric_counts(
        expected,
        actual,
        alignment,
        critical_paths,
        expected_ingestion_decision,
    )
    return {
        "compared_fields": len(matched) + len(mismatches),
        "matched_fields": len(matched),
        "mismatched_fields": len(mismatches),
        "matched_paths": sorted(matched),
        "mismatched_paths": [entry["field_path"] for entry in mismatches],
        "critical_mismatched_fields": len(critical_paths),
        "critical_mismatched_paths": critical_paths,
        "pipeline_decision_before_gold_comparison": decision,
        "false_auto_accept": false_auto_accept,
        "case_type": case_type,
        "expected_ingestion_decision": expected_ingestion_decision,
        "semantic_item_alignment": alignment,
        "layers": layers,
        "metric_counts": metric_counts,
        "mismatches": mismatches,
    }


def align_prescription_items(
    expected_items: list[dict[str, Any]],
    actual_items: list[dict[str, Any]],
) -> dict[str, Any]:
    """Align without array position; ambiguous duplicates are never forced."""

    unmatched_expected = set(range(len(expected_items)))
    unmatched_actual = set(range(len(actual_items)))
    matched_pairs: list[tuple[int, int]] = []
    ambiguous_expected: set[int] = set()
    ambiguous_actual: set[int] = set()
    while unmatched_expected and unmatched_actual:
        scores = {
            (expected_index, actual_index): _item_alignment_score(
                expected_items[expected_index], actual_items[actual_index]
            )
            for expected_index in unmatched_expected
            for actual_index in unmatched_actual
        }
        candidates: list[tuple[int, int, int]] = []
        for expected_index in sorted(unmatched_expected):
            ranked = sorted(
                (
                    (score, actual_index)
                    for (candidate_expected, actual_index), score in scores.items()
                    if candidate_expected == expected_index and score > 0
                ),
                reverse=True,
            )
            if not ranked:
                continue
            top_score = ranked[0][0]
            top_actual = [index for score, index in ranked if score == top_score]
            if len(top_actual) != 1:
                ambiguous_expected.add(expected_index)
                ambiguous_actual.update(top_actual)
                continue
            actual_index = top_actual[0]
            reverse_ranked = sorted(
                (
                    (score, candidate_expected)
                    for (candidate_expected, candidate_actual), score in scores.items()
                    if candidate_actual == actual_index and score > 0
                ),
                reverse=True,
            )
            reverse_top = [index for score, index in reverse_ranked if score == top_score]
            if len(reverse_top) == 1 and reverse_top[0] == expected_index:
                candidates.append((top_score, expected_index, actual_index))
            else:
                ambiguous_expected.add(expected_index)
                ambiguous_actual.add(actual_index)
        if not candidates:
            break
        for _, expected_index, actual_index in sorted(candidates, reverse=True):
            if expected_index in unmatched_expected and actual_index in unmatched_actual:
                matched_pairs.append((expected_index, actual_index))
                unmatched_expected.remove(expected_index)
                unmatched_actual.remove(actual_index)
    ambiguous = bool(ambiguous_expected or ambiguous_actual)
    return {
        "strategy": "canonical_drug_name+processing_method_then_dose_package_original_text",
        "matched_pairs": sorted(matched_pairs),
        "missing_expected_indices": sorted(unmatched_expected - ambiguous_expected),
        "unexpected_actual_indices": sorted(unmatched_actual - ambiguous_actual),
        "ambiguous_expected_indices": sorted(ambiguous_expected & unmatched_expected),
        "ambiguous_actual_indices": sorted(ambiguous_actual & unmatched_actual),
        "ambiguous": ambiguous,
        "ambiguity_code": "AMBIGUOUS_ITEM_MATCH" if ambiguous else None,
    }


# Leading processing prefixes printed as part of the drug name (炒酸枣仁).
# The comparator tolerates this display form when aligning items; the parsers
# themselves keep the printed raw name unchanged.
_PROCESSING_PREFIXES = (
    "麸炒",
    "清炒",
    "姜炒",
    "醋炒",
    "酒炒",
    "炒",
    "炙",
    "煅",
    "姜",
    "醋",
    "酒",
    "盐",
    "蜜",
    "蒸",
    "烫",
    "煨",
    "制",
    "土",
)
_PROCESSING_PREFIX_PATTERN = re.compile(
    "(?:" + "|".join(_PROCESSING_PREFIXES) + r")(?=[\u4e00-\u9fff])"
)


def _raw_name_stem(value: str | None) -> tuple[str | None, str | None]:
    """Split a printed raw name into (stem, embedded processing prefix)."""

    if value is None:
        return None, None
    match = _PROCESSING_PREFIX_PATTERN.match(value)
    if match and len(value) > match.end():
        return value[match.end() :], value[: match.end()]
    return value, None


def _item_alignment_score(expected: dict[str, Any], actual: dict[str, Any]) -> int:
    expected_canonical = _comparison_text(expected.get("canonical_drug_name"))
    actual_canonical = _comparison_text(actual.get("canonical_drug_name"))
    expected_processing = _comparison_text(expected.get("processing_method"))
    actual_processing = _comparison_text(actual.get("processing_method"))
    expected_raw = _comparison_text(expected.get("raw_drug_name"))
    actual_raw = _comparison_text(actual.get("raw_drug_name"))
    score = 0
    if expected_canonical and expected_canonical == actual_canonical:
        score = 100 if expected_processing == actual_processing else 80
    elif expected_raw and expected_raw == actual_raw:
        score = 60 if expected_processing == actual_processing else 50
    else:
        # Display-form tolerance: 炒酸枣仁 (prefix embedded in the printed
        # name) aligns with 酸枣仁 + processing 炒. Alignment only — the
        # field-level comparison still reports the real differences.
        expected_stem, expected_prefix = _raw_name_stem(expected_raw)
        actual_stem, actual_prefix = _raw_name_stem(actual_raw)
        if not expected_stem or not actual_stem or expected_stem != actual_stem:
            return 0
        expected_effective = expected_processing or expected_prefix
        actual_effective = actual_processing or actual_prefix
        score = 45 if expected_effective == actual_effective else 35
    for field in (
        "explicit_package_dose_g",
        "package_count",
        "original_text",
    ):
        if expected.get(field) is not None and expected.get(field) == actual.get(field):
            score += 5
    return score


def benchmark_layer_for_path(path: str) -> str:
    if path == "prescription.item_alignment" or path.startswith("validation."):
        return "SAFETY_DECISION"
    if (
        path.endswith(("raw_drug_name", "original_text"))
        or ".raw_" in path
        or path.startswith(("ocr.", "source_extraction."))
    ):
        return "SOURCE_EXTRACTION"
    return "CLINICAL_NORMALIZATION"


def _mismatch(
    path: str,
    expected_paths: dict[str, Any],
    actual_paths: dict[str, Any],
) -> dict[str, Any]:
    return {
        "field_path": path,
        "layer": benchmark_layer_for_path(path),
        "critical": is_critical_field_path(path),
        "error_category": classify_mismatch(path, expected_paths, actual_paths),
        "classification_source": "automatic",
    }


def aggregate_benchmark_metrics(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in case_results:
        grouped[result.get("case_type") or "UNKNOWN"].append(result)
    tracks: dict[str, dict[str, Any]] = {}
    for case_type, results in sorted(grouped.items()):
        totals = {
            name: {"numerator": 0, "denominator": 0, "excluded": 0} for name in METRIC_DEFINITIONS
        }
        for result in results:
            for name, counts in result.get("metric_counts", {}).items():
                if name not in totals:
                    continue
                for key in ("numerator", "denominator", "excluded"):
                    totals[name][key] += int(counts.get(key, 0))
        metrics: dict[str, Any] = {}
        for name, counts in totals.items():
            denominator = counts["denominator"]
            metrics[name] = {
                **counts,
                "value": counts["numerator"] / denominator if denominator else None,
                "definition": METRIC_DEFINITIONS[name],
            }
        tracks[case_type] = {
            "case_count": len(results),
            "metrics": metrics,
        }
        if case_type.startswith("JNU_LEGACY"):
            safe_count = sum(
                _is_auto_accept(result.get("pipeline_decision_before_gold_comparison"))
                and not result.get("critical_mismatched_paths")
                for result in results
            )
            tracks[case_type]["safe_automated_coverage"] = {
                "numerator": safe_count,
                "denominator": len(results),
                "value": safe_count / len(results) if results else None,
            }
    return {"tracks": tracks, "definitions": METRIC_DEFINITIONS}


def _case_metric_counts(
    expected: dict[str, Any],
    actual: dict[str, Any],
    alignment: dict[str, Any],
    critical_paths: list[str],
    expected_ingestion_decision: str | None,
) -> dict[str, dict[str, int]]:
    counts = {
        name: {"numerator": 0, "denominator": 0, "excluded": 0} for name in METRIC_DEFINITIONS
    }
    expected_match = expected.get("patient_match")
    actual_match = actual.get("patient_match")
    if isinstance(expected_match, dict):
        counts["patient_match_accuracy"]["denominator"] = 1
        counts["patient_match_accuracy"]["numerator"] = int(
            isinstance(actual_match, dict)
            and expected_match.get("decision") == actual_match.get("decision")
            and expected_match.get("matched_case_id") == actual_match.get("matched_case_id")
        )
    else:
        counts["patient_match_accuracy"]["excluded"] = 1

    expected_date = expected.get("encounter", {}).get("encounter_date")
    actual_date = actual.get("encounter", {}).get("encounter_date")
    if expected_date is None:
        counts["encounter_date_accuracy"]["excluded"] = 1
    else:
        counts["encounter_date_accuracy"]["denominator"] = 1
        counts["encounter_date_accuracy"]["numerator"] = int(expected_date == actual_date)

    expected_items = _prescription_items(expected)
    actual_items = _prescription_items(actual)
    matched_pairs = alignment["matched_pairs"]
    counts["prescription_item_recall"].update(
        numerator=len(matched_pairs), denominator=len(expected_items)
    )
    counts["prescription_item_precision"].update(
        numerator=len(matched_pairs), denominator=len(actual_items)
    )
    field_metrics = {
        "drug_identity_accuracy": "canonical_drug_name",
        "processing_accuracy": "processing_method",
        "explicit_dose_accuracy": "explicit_package_dose_g",
        "package_count_accuracy": "package_count",
        "derived_dose_accuracy": "calculated_dose_g",
    }
    for expected_index, actual_index in matched_pairs:
        for metric, field in field_metrics.items():
            expected_value = expected_items[expected_index].get(field)
            if expected_value is None:
                counts[metric]["excluded"] += 1
                continue
            counts[metric]["denominator"] += 1
            counts[metric]["numerator"] += int(
                expected_value == actual_items[actual_index].get(field)
            )

    decision = actual.get("validation", {}).get("decision", "UNRESOLVED")
    if expected_ingestion_decision == "UNRESOLVED":
        counts["unresolved_recall"]["denominator"] = 1
        counts["unresolved_recall"]["numerator"] = int(_is_unresolved(decision))
    else:
        counts["unresolved_recall"]["excluded"] = 1
    if _is_auto_accept(decision):
        counts["auto_accept_precision"]["denominator"] = 1
        counts["auto_accept_precision"]["numerator"] = int(not critical_paths)
    else:
        counts["auto_accept_precision"]["excluded"] = 1
    counts["critical_false_auto_accept"]["denominator"] = 1
    counts["critical_false_auto_accept"]["numerator"] = int(
        _is_auto_accept(decision) and bool(critical_paths)
    )
    return counts


def _prescription_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("prescription", {}).get("items", [])
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("Prescription items must be a list of objects")
    return value


def _remove_prescription_items(payload: dict[str, Any]) -> None:
    prescription = payload.get("prescription")
    if isinstance(prescription, dict):
        prescription.pop("items", None)


def _comparison_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _is_auto_accept(decision: Any) -> bool:
    return str(decision).casefold() in {"auto_accept", "auto_accepted", "accepted"}


def _is_unresolved(decision: Any) -> bool:
    return str(decision).casefold() in {
        "unresolved",
        "review_required",
        "unresolved_no_manual_action",
    }


def is_critical_field_path(path: str) -> bool:
    if path in {
        "patient_identity.real_name",
        "patient_identity.medical_record_number",
        "patient_match.decision",
        "patient_match.matched_case_id",
        "encounter.encounter_date",
        "prescription.item_count",
    }:
        return True
    return bool(
        re.fullmatch(
            r"prescription\.items\[\d+\]\."
            r"(?:canonical_drug_name|processing_method|"
            r"explicit_package_dose_g|package_count|calculated_dose_g)",
            path,
        )
    )


def classify_mismatch(
    path: str,
    expected_paths: dict[str, Any],
    actual_paths: dict[str, Any],
) -> str:
    if path.startswith("patient_match."):
        return "PATIENT_MATCH_ERROR"
    if path.startswith("validation."):
        return "VALIDATION_POLICY_ERROR"
    if path.endswith("calculated_dose_g"):
        prefix = path.removesuffix("calculated_dose_g")
        explicit_path = prefix + "explicit_package_dose_g"
        if actual_paths.get(explicit_path) is None and expected_paths.get(path) is not None:
            return "REFERENCE_DATA_MISSING"
        return "NORMALIZATION_ERROR"
    if path.endswith("canonical_drug_name"):
        if actual_paths.get(path) is None and expected_paths.get(path) is not None:
            return "REFERENCE_DATA_MISSING"
        return "NORMALIZATION_ERROR"
    if path.endswith("processing_method"):
        return "NORMALIZATION_ERROR"
    if path.startswith(("patient_identity.", "patient.", "encounter.", "prescription.")):
        return "TEMPLATE_PARSER_ERROR"
    return "OTHER"


def evaluate_production_gate(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    false_auto_accepts = [result for result in case_results if result.get("false_auto_accept")]
    incomplete = [result for result in case_results if result.get("benchmark_completed") is False]
    auto_accepted_critical_errors: dict[str, int] = {
        "patient_match_or_mrn": 0,
        "encounter_date": 0,
        "missing_or_extra_prescription_item": 0,
        "drug_identity": 0,
        "explicit_dose": 0,
        "package_count": 0,
    }
    for result in case_results:
        if result.get("pipeline_decision_before_gold_comparison") != "auto_accept":
            continue
        for path in result.get("critical_mismatched_paths", []):
            target = _gate_target_for_path(path)
            if target:
                auto_accepted_critical_errors[target] += 1
    current_results = [
        result for result in case_results if result.get("case_type") == "JNU_CURRENT_V1"
    ]
    current_auto_accepted = sum(
        _is_auto_accept(result.get("pipeline_decision_before_gold_comparison"))
        and not result.get("critical_mismatched_paths")
        for result in current_results
    )
    if incomplete:
        status = "NOT_READY_INCOMPLETE_BENCHMARK"
    elif len(case_results) < PRODUCTION_GATE_MINIMUM_REPRESENTATIVE_CASES:
        status = "NOT_READY_INSUFFICIENT_REPRESENTATIVE_CASES"
    elif current_auto_accepted < CURRENT_PRODUCTION_GATE_MINIMUM_AUTO_ACCEPTED_CASES:
        status = "NOT_READY_INSUFFICIENT_CURRENT_AUTO_ACCEPTED_CASES"
    elif false_auto_accepts:
        status = "NOT_READY_CRITICAL_FALSE_AUTO_ACCEPT"
    elif any(auto_accepted_critical_errors.values()):
        status = "NOT_READY_CRITICAL_TARGET_FAILED"
    else:
        status = "CANDIDATE_REQUIRES_OPERATIONAL_SIGN_OFF"
    return {
        "status": status,
        "production_ready": False,
        "case_count": len(case_results),
        "minimum_representative_cases": PRODUCTION_GATE_MINIMUM_REPRESENTATIVE_CASES,
        "target_range": "20-30 representative confirmed cases",
        "critical_false_auto_accept_count": len(false_auto_accepts),
        "incomplete_case_count": len(incomplete),
        "auto_accepted_critical_error_counts": auto_accepted_critical_errors,
        "current_auto_accepted_without_critical_error": current_auto_accepted,
        "minimum_current_auto_accepted_cases": (
            CURRENT_PRODUCTION_GATE_MINIMUM_AUTO_ACCEPTED_CASES
        ),
        "review_required_as_default_terminal_workflow": False,
        "unresolved_cases_may_remain_without_manual_action": True,
        "five_case_purpose": "finding_failure_modes_only",
        "separate_track_metrics": aggregate_benchmark_metrics(case_results),
    }


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _validate_explicit_case_selection(selected_case_ids: list[str]) -> None:
    if not selected_case_ids:
        raise ValueError("At least one explicit case ID is required")
    if len(selected_case_ids) != len(set(selected_case_ids)):
        raise ValueError("Selected case IDs must be unique")


def _resolve_relative_path(root: Path, raw_path: Any, *, must_exist: bool) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("Manifest image/gold paths must be non-empty strings")
    relative = Path(raw_path)
    if relative.is_absolute():
        raise ValueError("Golden manifest paths must be relative")
    candidate = (root / relative).resolve(strict=must_exist)
    if must_exist:
        return _safe_file(root, candidate)
    _assert_safe_path(root, candidate)
    return candidate


def _safe_file(root: Path, candidate: Path) -> Path:
    _assert_safe_path(root, candidate)
    if not candidate.is_file():
        raise ValueError("Golden dataset input must be a regular file")
    return candidate


def _assert_safe_path(root: Path, candidate: Path) -> None:
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("Golden dataset path escapes the private root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError("Golden dataset symlinks are not allowed")


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        flattened: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError("Expected-field keys must be non-empty strings")
            path = f"{prefix}.{key}" if prefix else key
            flattened.update(_flatten(child, path))
        return flattened
    if isinstance(value, list):
        flattened = {}
        for index, child in enumerate(value):
            path = f"{prefix}[{index}]"
            flattened.update(_flatten(child, path))
        return flattened
    if not prefix:
        raise ValueError("Expected benchmark content must be an object")
    return {prefix: value}


def _decimal_string(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized


def _issue(issue_code: str, field_path: str) -> dict[str, str]:
    return {"issue_code": issue_code, "field_path": field_path}


def has_suspicious_prescription_residual(raw_text: str | None) -> bool:
    """Fail-closed check that no unexplained drug-zone content remains.

    Phase 6.1 fix (generic, not layout-specific): the OCR source and the AI
    evidence are folded through the same normalization (MinerU markup
    flattened, NFKC, per-line clause splitting on whitespace) before the
    safe-pattern comparison, so instruction lines the OCR or the extractor
    merged onto one physical line no longer trigger a false residual.
    A line is explained only when EVERY clause matches a safe pattern; any
    unmatched clause — including drug-like fragments — still fails closed.

    OCR spaces can also shatter one phrase into fragments ("共 7 付(13味)"
    -> 共 / 7 / 付(13味)), so an unexplained clause is re-joined with its
    successor and re-checked. Every safe pattern is start-anchored, so only
    a benign phrase prefix can become explained by merging, while a
    drug-residue clause never matches however far it is extended; a merge
    never re-opens an already-explained clause to absorb later content.
    """

    if not raw_text:
        return False
    safe_patterns = (
        r"^共\s*\d{1,3}\s*[剂付]$",
        r"^(?:用法|服法)\s*[:：].+$",
        r"^每日\s*\d+(?:\.\d+)?\s*[剂次].*$",
        r"^每日\s*\d+(?:\.\d+)?\s*剂.*$",
        r"^每日\s*\d+(?:\.\d+)?\s*次.*$",
        r"^一次(?:用量)?\s*[:：]?\s*\d+(?:\.\d+)?\s*(?:ml|毫升).*$",
        r"^(?:水煎|煎服|分\s*\d+\s*次服|饭[前后]服).*$",
        # Legacy prescription-slip instruction/noise forms (v0.2.x): 付
        # counting with a (N味) summary, billing/pharmacy footers, banners,
        # and standalone per-item decoction notes. Bounded and observed only
        # on 中药处方笺 layouts; the current outpatient record never carries
        # them, so the conservative gate is not weakened for it.
        r"^共\s*\d{1,3}\s*付.*$",
        r"^\d{1,3}\s*[剂付]\s*(?:用法|服法).*$",
        r"^(?:药价|公费记账|付现|药品金额|收费|金额)\s*[:：].*$",
        r"^(?:审\s*方|核对(?:发药)?|调\s*配|调配|调剂|打印|处方当日).*$",
        r"^请微信扫码缴费$",
        r"^(?:先煎|后下|烊化|冲服|包煎|另煎|兑服)$",
    )

    def _clause_explained(clause: str) -> bool:
        return any(re.fullmatch(pattern, clause) for pattern in safe_patterns)

    for raw_line in raw_text.splitlines():
        flattened = re.sub(r"<[^>]+>", " ", raw_line)
        folded = unicodedata.normalize("NFKC", flattened)
        clauses = [clause.strip() for clause in folded.split() if clause.strip()]
        if not clauses:
            continue
        if all(_clause_explained(clause) for clause in clauses):
            continue
        pending = ""
        for clause in clauses:
            candidate = f"{pending}{re.sub(r'\s+', '', clause)}"
            pending = "" if _clause_explained(candidate) else candidate
        if not pending:
            continue
        return True
    return False


def _gate_target_for_path(path: str) -> str | None:
    if path.startswith("patient_match.") or path.startswith("patient_identity."):
        return "patient_match_or_mrn"
    if path == "encounter.encounter_date":
        return "encounter_date"
    if (
        path == "prescription.item_count"
        or path.startswith(("prescription.items[", "prescription.unexpected_items["))
        and path.count(".") == 1
    ):
        return "missing_or_extra_prescription_item"
    if path.endswith(("raw_drug_name", "canonical_drug_name", "processing_method")):
        return "drug_identity"
    if path.endswith("explicit_package_dose_g"):
        return "explicit_dose"
    if path.endswith("package_count"):
        return "package_count"
    if path.endswith("calculated_dose_g"):
        return "explicit_dose"
    return None


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("Optional manifest string fields must be non-empty strings or null")
    return value


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError("Manifest test_intents must be a list of non-empty strings")
    return tuple(value)
