from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = PROJECT_ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT))

from app.core.config import get_settings
from app.services.calibration import (
    PatientMatchTracker,
    build_assisted_gold_draft,
    build_normalized_clinical_result,
    compare_clinical_benchmark,
    evaluate_production_gate,
    load_selected_case_references,
    load_selected_golden_cases,
    normalize_ocr_text,
    validate_first_five_manifest,
    verify_assisted_candidate_snapshot,
    write_json_atomic,
)
from app.services.ocr.base import OCRProviderError
from app.services.ocr.mineru import MinerUOCRProvider
from app.services.parsers.detector import (
    CURRENT_TEMPLATE,
    LEGACY_TEMPLATE,
    UNKNOWN_TEMPLATE,
    detect_template,
)
from app.services.parsers.jnu import (
    JNUHospitalOutpatientParser,
    JNUParserError,
)
from app.services.parsers.jnu_legacy import (
    JNULegacyParserError,
    JNULegacyPrescriptionParser,
)

PRIVATE_ROOT = (PROJECT_ROOT / "private_data" / "clinical_golden").resolve()
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.production.local"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Explicit private MinerU/JNU calibration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    status_parser = subparsers.add_parser("manifest-status")
    status_parser.add_argument("--manifest", type=Path, required=True)
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--manifest", type=Path, required=True)
    validate_parser.add_argument("--case-id", action="append", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--manifest", type=Path, required=True)
    run_parser.add_argument("--case-id", action="append", required=True)
    run_parser.add_argument("--submit", action="store_true", required=True)
    run_parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    run_parser.add_argument("--prefill-annotation-draft", action="store_true")
    compare_parser = subparsers.add_parser("compare-run")
    compare_parser.add_argument("--manifest", type=Path, required=True)
    compare_parser.add_argument("--case-id", action="append", required=True)
    compare_parser.add_argument("--run-id", required=True)
    reparse_parser = subparsers.add_parser("reparse-run")
    reparse_parser.add_argument("--manifest", type=Path, required=True)
    reparse_parser.add_argument("--case-id", action="append", required=True)
    reparse_parser.add_argument("--run-id", required=True)
    reparse_parser.add_argument("--refresh-unedited-assisted-draft", action="store_true")
    return parser


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_private_environment(path: Path) -> None:
    env_path = path.expanduser().resolve(strict=True)
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError("Production environment file contains an invalid line")
        name, value = line.split("=", 1)
        os.environ[name.strip()] = _unquote_env_value(value.strip())
    get_settings.cache_clear()


def _unquote_env_value(value: str) -> str:
    """The production env file uses Compose-safe literal quoting."""

    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("\\'", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def _parse_with_routed_parser(
    normalized_ocr: str,
) -> tuple[object | None, str | None, str]:
    """Route OCR text to exactly one template parser (no cross-hacks)."""

    detection = detect_template(normalized_ocr)
    if detection.template == CURRENT_TEMPLATE:
        try:
            return JNUHospitalOutpatientParser().parse(normalized_ocr), None, detection.template
        except JNUParserError:
            return None, "JNU_PARSER_INCOMPLETE", detection.template
    if detection.template == LEGACY_TEMPLATE:
        try:
            return JNULegacyPrescriptionParser().parse(normalized_ocr), None, detection.template
        except JNULegacyParserError:
            return None, "LEGACY_PARSER_INCOMPLETE", detection.template
    return None, "UNSUPPORTED_TEMPLATE", UNKNOWN_TEMPLATE


def _parser_failure_actual(reason_code: str, template: str) -> dict[str, Any]:
    return {
        "patient_identity": {"real_name": None, "medical_record_number": None},
        "patient": {"sex": "unknown"},
        "encounter": {
            "age": None,
            "encounter_date": None,
            "chief_complaint": None,
            "past_history": None,
            "diagnosis": [],
            "physician": None,
        },
        "prescription": {"item_count": 0, "total_decoctions": None, "items": []},
        "patient_match": {"decision": "review_required", "matched_case_id": None},
        "validation": {
            "review_flags": [reason_code],
            "decision": "unresolved",
            "blocking_review_reasons": [reason_code],
            "non_blocking_warnings": [],
        },
        "document": {"parser_template": template},
    }


def _parser_failure_validation(reason_code: str) -> dict[str, Any]:
    return {
        "status": "unresolved",
        "issues": [{"issue_code": reason_code, "field_path": "document.ocr_text"}],
        "review_flags": [reason_code],
        "critical_checks_fail_closed": True,
    }


def _parser_failure_decision(reason_code: str) -> dict[str, Any]:
    return {
        "decision": "unresolved",
        "review_issue_count": 1,
        "blocking_review_reasons": [reason_code],
        "non_blocking_warnings": [],
        "auto_accept_threshold_relaxed": False,
    }


def _case_report_base(case) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "document_id": str(case.document_id),
        "case_type": case.case_type,
        "expected_ingestion_decision": case.expected_ingestion_decision,
        "test_intents": list(case.test_intents),
        "provider": "mineru",
        "status": "failed",
        "error_code": None,
        "benchmark_completed": False,
        "pipeline_decision_before_gold_comparison": "unresolved",
        "false_auto_accept": False,
        "critical_mismatched_paths": [],
    }


def _load_gold_state(case) -> tuple[str, dict[str, Any] | None]:
    if not case.gold_path.is_file():
        return "missing", None
    payload = json.loads(case.gold_path.read_text(encoding="utf-8"))
    expected = payload.get("expected")
    annotation = payload.get("annotation", {})
    if payload.get("schema_version") not in {"1.0", "2.0", "3.0"} or not isinstance(
        expected, dict
    ):
        raise ValueError(f"Gold JSON for {case.case_id} is invalid")
    status = annotation.get("status", "legacy") if isinstance(annotation, dict) else "legacy"
    return str(status), expected if status == "confirmed" else None


def _apply_confirmed_gold_or_create_draft(
    case,
    actual: dict[str, Any],
    *,
    candidate_path: Path,
    run_id: str,
    prefill_annotation_draft: bool,
    case_report: dict[str, Any],
    refresh_unedited_assisted_draft: bool = False,
) -> None:
    gold_status, confirmed_expected = _load_gold_state(case)
    case_report["gold_status_at_run"] = gold_status
    case_report["pipeline_decision_before_gold_comparison"] = actual.get(
        "validation", {}
    ).get("decision", "unresolved")
    if confirmed_expected is not None:
        comparison = compare_clinical_benchmark(
            confirmed_expected,
            actual,
            case_type=case.case_type,
            expected_ingestion_decision=case.expected_ingestion_decision,
        )
        write_json_atomic(candidate_path.parent / "comparison_with_gold.json", comparison)
        case_report.update({"benchmark_completed": True, **comparison})
        return

    case_report["benchmark_completed"] = False
    case_report["annotation_draft_created"] = False
    if not prefill_annotation_draft:
        return
    if case.gold_path.exists():
        existing = json.loads(case.gold_path.read_text(encoding="utf-8"))
        annotation = existing.get("annotation", {})
        refreshable = (
            refresh_unedited_assisted_draft
            and isinstance(annotation, dict)
            and annotation.get("status") == "draft"
            and annotation.get("entry_mode") == "mineru_assisted_correction"
            and "human_verified_all_fields" not in annotation
        )
        if not refreshable:
            case_report["annotation_draft_status"] = "existing_unconfirmed_gold_preserved"
            return
        history = (
            case.gold_path.parent
            / "history"
            / case.case_id
            / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        )
        history.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(case.gold_path, history)
    draft = build_assisted_gold_draft(
        case,
        actual,
        run_id=run_id,
        candidate_snapshot_reference=candidate_path.relative_to(PRIVATE_ROOT).as_posix(),
        candidate_snapshot_sha256=_sha256_file(candidate_path),
        created_at=_now(),
    )
    write_json_atomic(case.gold_path, draft)
    case_report["annotation_draft_created"] = True
    case_report["annotation_draft_status"] = (
        "mineru_assisted_candidate_refreshed"
        if gold_status == "draft"
        else "mineru_assisted_correction_required"
    )


def _compare_frozen_run(args, manifest: Path) -> int:
    cases = load_selected_golden_cases(
        manifest,
        PRIVATE_ROOT,
        args.case_id,
        require_confirmed=True,
    )
    run_root = _resolve_run_root(args.run_id)
    comparison_id = (
        f"comparison-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    )
    compared_cases: list[dict[str, Any]] = []
    for case in cases:
        gold_payload = json.loads(case.gold_path.read_text(encoding="utf-8"))
        annotation = gold_payload.get("annotation", {})
        candidate_reference = None
        if (
            isinstance(annotation, dict)
            and annotation.get("candidate_run_id") == args.run_id
            and isinstance(annotation.get("candidate_snapshot_reference"), str)
        ):
            candidate_reference = annotation["candidate_snapshot_reference"]
        candidate_path = (
            (PRIVATE_ROOT / candidate_reference).resolve(strict=True)
            if candidate_reference
            else (run_root / case.case_id / "normalized_clinical_result.json").resolve(
                strict=True
            )
        )
        try:
            candidate_path.relative_to(run_root)
        except ValueError as exc:
            raise SystemExit("Frozen candidate path escapes the selected run") from exc
        actual = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate_sha256 = _sha256_file(candidate_path)
        try:
            verify_assisted_candidate_snapshot(
                annotation,
                run_id=args.run_id,
                actual_sha256=candidate_sha256,
            )
        except ValueError as exc:
            raise SystemExit(
                f"{case.case_id}: frozen candidate integrity verification failed"
            ) from exc
        comparison = compare_clinical_benchmark(
            case.expected,
            actual,
            case_type=case.case_type,
            expected_ingestion_decision=case.expected_ingestion_decision,
        )
        comparison_payload = {
            "schema_version": "1.0",
            "comparison_id": comparison_id,
            "run_id": args.run_id,
            "case_id": case.case_id,
            "compared_at": _now(),
            "candidate_snapshot_sha256": candidate_sha256,
            "gold_snapshot_sha256": _sha256_file(case.gold_path),
            **comparison,
        }
        comparison_path = (
            run_root / case.case_id / "comparisons" / f"{comparison_id}.json"
        )
        write_json_atomic(comparison_path, comparison_payload)
        compared_cases.append(
            {
                "case_id": case.case_id,
                "benchmark_completed": True,
                "comparison": comparison_path.relative_to(PRIVATE_ROOT).as_posix(),
                **comparison,
            }
        )
    gate = evaluate_production_gate(compared_cases)
    report_path = PRIVATE_ROOT / "reports" / f"{comparison_id}.json"
    write_json_atomic(
        report_path,
        {
            "schema_version": "1.0",
            "comparison_id": comparison_id,
            "run_id": args.run_id,
            "created_at": _now(),
            "contains_pii": False,
            "cases": compared_cases,
            "production_gate": gate,
        },
    )
    print(
        json.dumps(
            {
                "status": "frozen_run_compared_with_confirmed_gold",
                "comparison_id": comparison_id,
                "case_count": len(compared_cases),
                "critical_false_auto_accept_count": gate[
                    "critical_false_auto_accept_count"
                ],
                "production_ready": False,
                "report": report_path.relative_to(PROJECT_ROOT).as_posix(),
            },
            sort_keys=True,
        )
    )
    return 0


def _resolve_run_root(run_id: str) -> Path:
    if not re.fullmatch(r"run-\d{8}T\d{6}Z-[0-9a-f]{8}", run_id):
        raise SystemExit("run-id has an invalid format")
    run_root = (PRIVATE_ROOT / "runs" / run_id).resolve(strict=True)
    try:
        run_root.relative_to(PRIVATE_ROOT)
    except ValueError as exc:
        raise SystemExit("run-id escapes the private calibration root") from exc
    if not run_root.is_dir():
        raise SystemExit("run-id does not identify a calibration directory")
    return run_root


def _reparse_frozen_run(args, manifest: Path) -> int:
    cases = load_selected_case_references(
        manifest,
        PRIVATE_ROOT,
        args.case_id,
        require_image=True,
    )
    run_root = _resolve_run_root(args.run_id)
    revision_id = f"reparse-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    revision_root = run_root / "reprocess" / revision_id
    revision_root.mkdir(parents=True, exist_ok=False)
    match_tracker = PatientMatchTracker()
    report_cases: list[dict[str, Any]] = []
    for case in cases:
        source_ocr_path = (run_root / case.case_id / "normalized_ocr.txt").resolve(strict=True)
        try:
            source_ocr_path.relative_to(run_root)
        except ValueError as exc:
            raise SystemExit("Frozen OCR path escapes the selected run") from exc
        case_root = revision_root / case.case_id
        case_root.mkdir(parents=True, exist_ok=False)
        write_json_atomic(
            case_root / "source_ocr_reference.json",
            {
                "case_id": case.case_id,
                "run_id": args.run_id,
                "source_ocr_reference": source_ocr_path.relative_to(PRIVATE_ROOT).as_posix(),
                "source_ocr_sha256": _sha256_file(source_ocr_path),
                "network_submission_performed": False,
            },
        )
        case_report = _case_report_base(case)
        case_report["network_submission_performed"] = False
        normalized_ocr = source_ocr_path.read_text(encoding="utf-8")
        parsed, parse_failure_code, detected_template = _parse_with_routed_parser(normalized_ocr)
        if parsed is not None:
            write_json_atomic(case_root / "parser_output.json", parsed.to_json())
            patient_match = match_tracker.evaluate(
                case.case_id,
                parsed.medical_record_number,
            )
            actual, validation, decision = build_normalized_clinical_result(
                parsed,
                patient_match=patient_match,
            )
            case_report.update(
                {
                    "status": "reparsed",
                    "detected_template": detected_template,
                    "parser_version": parsed.parser_version,
                    "error_code": None,
                }
            )
        else:
            actual = _parser_failure_actual(parse_failure_code, detected_template)
            validation = _parser_failure_validation(parse_failure_code)
            decision = _parser_failure_decision(parse_failure_code)
            case_report.update(
                {
                    "status": "reparsed_with_parser_failure",
                    "detected_template": detected_template,
                    "error_code": parse_failure_code,
                }
            )
        candidate_path = case_root / "normalized_clinical_result.json"
        write_json_atomic(candidate_path, actual)
        write_json_atomic(case_root / "validation_result.json", validation)
        write_json_atomic(case_root / "auto_accept_review_decision.json", decision)
        _apply_confirmed_gold_or_create_draft(
            case,
            actual,
            candidate_path=candidate_path,
            run_id=args.run_id,
            prefill_annotation_draft=True,
            case_report=case_report,
            refresh_unedited_assisted_draft=args.refresh_unedited_assisted_draft,
        )
        report_cases.append(case_report)

    gate = evaluate_production_gate(report_cases)
    report_path = PRIVATE_ROOT / "reports" / f"{revision_id}.json"
    write_json_atomic(
        report_path,
        {
            "schema_version": "1.0",
            "revision_id": revision_id,
            "run_id": args.run_id,
            "created_at": _now(),
            "contains_pii": False,
            "network_submission_performed": False,
            "cases": report_cases,
            "production_gate": gate,
        },
    )
    print(
        json.dumps(
            {
                "status": "frozen_ocr_reparsed",
                "run_id": args.run_id,
                "revision_id": revision_id,
                "case_count": len(report_cases),
                "benchmark_completed_cases": sum(
                    bool(case.get("benchmark_completed")) for case in report_cases
                ),
                "network_submission_performed": False,
                "production_ready": False,
                "report": report_path.relative_to(PROJECT_ROOT).as_posix(),
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    args = build_parser().parse_args()
    manifest = args.manifest.resolve(strict=True)
    if args.command == "manifest-status":
        status = validate_first_five_manifest(manifest, PRIVATE_ROOT)
        print(json.dumps(status, sort_keys=True))
        return 0

    if args.command == "validate":
        cases = load_selected_golden_cases(
            manifest,
            PRIVATE_ROOT,
            args.case_id,
            require_confirmed=True,
        )
        print(
            json.dumps(
                {
                    "status": "validated_no_network",
                    "selected_case_count": len(cases),
                    "all_gold_confirmed": True,
                    "automatic_directory_traversal": False,
                },
                sort_keys=True,
            )
        )
        return 0
    if args.command == "compare-run":
        return _compare_frozen_run(args, manifest)
    if args.command == "reparse-run":
        return _reparse_frozen_run(args, manifest)

    cases = load_selected_case_references(
        manifest,
        PRIVATE_ROOT,
        args.case_id,
        require_image=True,
    )

    _load_private_environment(args.env_file)
    settings = get_settings()
    if not settings.is_production:
        raise SystemExit("APP_ENV must be production for live clinical calibration")
    if settings.ocr_provider != "mineru":
        raise SystemExit("OCR_PROVIDER must be mineru for live calibration")
    if not settings.allow_identifiable_mineru_upload:
        raise SystemExit("ALLOW_IDENTIFIABLE_MINERU_UPLOAD must be true for live calibration")

    run_id = f"run-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_root = PRIVATE_ROOT / "runs" / run_id
    report_cases: list[dict[str, Any]] = []
    provider = MinerUOCRProvider()
    match_tracker = PatientMatchTracker()

    for case in cases:
        case_root = run_root / case.case_id
        case_root.mkdir(parents=True, exist_ok=False)
        source_reference = {
            "case_id": case.case_id,
            "document_id": str(case.document_id),
            "source_image_reference": case.image_path.relative_to(PRIVATE_ROOT).as_posix(),
            "source_sha256": _sha256_file(case.image_path),
            "source_bytes": case.image_path.stat().st_size,
        }
        write_json_atomic(case_root / "source_reference.json", source_reference)
        audit_path = case_root / "submission_audit.json"
        audit: dict[str, Any] = {
            "document_id": str(case.document_id),
            "provider": provider.name,
            "submitted_at": _now(),
            "completed_at": None,
            "status": "submitted",
            "request_job_identifier": None,
        }
        write_json_atomic(audit_path, audit)

        def record_request_id(
            request_id: str,
            *,
            current_audit: dict[str, Any] = audit,
            current_audit_path: Path = audit_path,
        ) -> None:
            current_audit["request_job_identifier"] = request_id
            current_audit["status"] = "running"
            write_json_atomic(current_audit_path, current_audit)

        case_report = _case_report_base(case)
        detected_template = UNKNOWN_TEMPLATE
        try:
            result = provider.extract(case.image_path, on_submitted=record_request_id)
            write_json_atomic(case_root / "mineru_raw_response.json", result.raw_result)
            (case_root / "ocr_source.md").write_text(result.text, encoding="utf-8")
            normalized_ocr = normalize_ocr_text(result.text)
            (case_root / "normalized_ocr.txt").write_text(normalized_ocr, encoding="utf-8")
            parsed, parse_failure_code, detected_template = _parse_with_routed_parser(
                normalized_ocr
            )
            if parsed is None:
                raise JNUParserError(parse_failure_code)
            parser_output = parsed.to_json()
            write_json_atomic(case_root / "parser_output.json", parser_output)
            patient_match = match_tracker.evaluate(
                case.case_id,
                parsed.medical_record_number,
            )
            normalized, validation, decision = build_normalized_clinical_result(
                parsed,
                patient_match=patient_match,
            )
            write_json_atomic(case_root / "normalized_clinical_result.json", normalized)
            write_json_atomic(case_root / "validation_result.json", validation)
            write_json_atomic(case_root / "auto_accept_review_decision.json", decision)
            case_report.update(
                {
                    "status": "completed",
                    "detected_template": detected_template,
                    "provider_version": result.provider_version,
                    "parser_version": parsed.parser_version,
                }
            )
            _apply_confirmed_gold_or_create_draft(
                case,
                normalized,
                candidate_path=case_root / "normalized_clinical_result.json",
                run_id=run_id,
                prefill_annotation_draft=args.prefill_annotation_draft,
                case_report=case_report,
            )
            audit["status"] = "completed"
        except OCRProviderError as exc:
            case_report["error_code"] = exc.code
            audit["status"] = "failed"
            audit["failure_code"] = exc.code
        except JNUParserError as exc:
            actual = _parser_failure_actual(str(exc), detected_template)
            validation = _parser_failure_validation(str(exc))
            decision = _parser_failure_decision(str(exc))
            write_json_atomic(case_root / "normalized_clinical_result.json", actual)
            write_json_atomic(case_root / "validation_result.json", validation)
            write_json_atomic(case_root / "auto_accept_review_decision.json", decision)
            case_report.update(
                {
                    "status": "completed_with_parser_failure",
                    "detected_template": detected_template,
                    "error_code": str(exc),
                }
            )
            _apply_confirmed_gold_or_create_draft(
                case,
                actual,
                candidate_path=case_root / "normalized_clinical_result.json",
                run_id=run_id,
                prefill_annotation_draft=args.prefill_annotation_draft,
                case_report=case_report,
            )
            audit["status"] = "completed_with_parser_failure"
        finally:
            audit["completed_at"] = _now()
            write_json_atomic(audit_path, audit)
        report_cases.append(case_report)

    production_gate = evaluate_production_gate(report_cases)
    report = {
        "schema_version": "2.0",
        "run_id": run_id,
        "created_at": _now(),
        "contains_pii": False,
        "case_count": len(report_cases),
        "benchmark_completed_cases": sum(
            bool(case.get("benchmark_completed")) for case in report_cases
        ),
        "critical_false_auto_accept_count": sum(
            bool(case.get("false_auto_accept")) for case in report_cases
        ),
        "cases": report_cases,
        "production_gate": production_gate,
    }
    report_path = PRIVATE_ROOT / "reports" / f"{run_id}.json"
    write_json_atomic(report_path, report)
    print(
        json.dumps(
            {
                "status": (
                    "completed"
                    if report["benchmark_completed_cases"] == len(report_cases)
                    else "completed_awaiting_human_gold_confirmation"
                ),
                "run_id": run_id,
                "case_count": len(report_cases),
                "benchmark_completed_cases": report["benchmark_completed_cases"],
                "critical_false_auto_accept_count": report[
                    "critical_false_auto_accept_count"
                ],
                "production_ready": False,
                "report": str(report_path.relative_to(PROJECT_ROOT)),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
