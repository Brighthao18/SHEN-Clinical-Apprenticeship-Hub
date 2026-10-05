"""Quick validation of the Phase 6 AI-primary ingestion chain.

Runs the explicitly selected calibration cases (default CASE001-CASE005)
offline through the new main chain:

    frozen MinerU OCR -> ClinicalAIExtractor (mock/fixture) -> Validator
    -> Patient/Drug/Dose Resolution -> IMPORTED / UNRESOLVED

Hard rules honored by this script:

- Case selection is explicit (`--case-id`); there is no private-directory
  scan and no MinerU submission. Only the frozen per-case `normalized_ocr.txt`
  of the chosen run is read.
- If no real AI provider is configured (current state: mock provider only),
  the script runs on extraction fixtures and says so. Cases without a
  matching fixture are reported as SKIP, never fabricated.
- The output is a plumbing/redaction-safe status report. It is NOT an AI
  accuracy claim. The eight zero-error usage gates (wrong patient match,
  encounter date, missing/unexpected items, canonical drug, explicit dose,
  package count, critical false auto-accept) still require Gold comparison
  under a real approved provider.

Example:
    python scripts/quick_validate_ai_ingestion.py \
        --run-id run-20260829T094035Z-33bdb030 \
        --case-id CASE001 --case-id CASE002 --case-id CASE003 \
        --case-id CASE004 --case-id CASE005
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = PROJECT_ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT))

# Isolated test-profile environment BEFORE app imports: in-memory SQLite,
# work-local storage, mock OCR, eager processing. No development or
# production data is touched.
os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["TASKS_EAGER"] = "true"
os.environ["OCR_PROVIDER"] = "mock"
os.environ["AUTH_DISABLED"] = "true"
os.environ["STORAGE_ROOT"] = str(PROJECT_ROOT / "work" / "quick-validate-storage")

PRIVATE_ROOT = (PROJECT_ROOT / "private_data" / "clinical_golden").resolve()
RUNS_ROOT = PRIVATE_ROOT / "runs"

from app.core.config import get_settings
from app.services.ai.extractor import evidence_key
from app.services.calibration import (
    load_selected_case_references,
    write_json_atomic,
)

CASE_DIR_SAFE_NAME = {"CASE"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline AI-primary ingestion quick validation (fixture-driven)"
    )
    parser.add_argument("--run-id", required=True, help="Frozen run directory under runs/")
    parser.add_argument("--case-id", action="append", required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Optional golden manifest to annotate expected_ingestion_decision",
    )
    parser.add_argument(
        "--fixture-registry",
        type=Path,
        help="Extraction fixture registry (default: the repository test registry)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Redacted JSON output path (default: outputs/quick_validate_ai_ingestion_<ts>.json)",
    )
    parser.add_argument(
        "--fallback-baseline",
        action="store_true",
        help="Additionally run each case through the preserved deterministic fallback "
        "parsers (no AI) to prove the reworked validator/resolution chain still "
        "handles the real frozen OCR fail-closed",
    )
    return parser


def _validate_case_ids(case_ids: list[str]) -> None:
    for case_id in case_ids:
        if not case_id.startswith("CASE") or not case_id[4:].isdigit() or "_" in case_id:
            raise ValueError(f"Case IDs must look like CASE012, got: {case_id}")


def _read_frozen_ocr(run_root: Path, case_id: str) -> str:
    ocr_path = (run_root / case_id / "normalized_ocr.txt").resolve(strict=True)
    if RUNS_ROOT not in ocr_path.parents:
        raise ValueError(f"Refusing to read outside the frozen runs directory: {ocr_path}")
    return ocr_path.read_text(encoding="utf-8")


def _load_expected_decisions(
    manifest_path: Path | None, case_ids: list[str]
) -> dict[str, str | None]:
    if manifest_path is None:
        return {case_id: None for case_id in case_ids}
    references = load_selected_case_references(
        manifest_path, PRIVATE_ROOT, case_ids, require_image=False
    )
    return {ref.case_id: ref.expected_ingestion_decision for ref in references}


def _fixture_keys(registry_path: Path | None) -> dict[str, dict[str, Any]]:
    path = registry_path or get_settings().ai_extraction_fixture_path
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        entries = dict(payload["entries"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Fixture registry {path} must contain an entries object") from exc
    return entries


def _run_case(client, ocr_text: str) -> dict[str, Any]:
    """Drive one document through the real upload -> pipeline path."""

    response = client.post(
        "/api/v1/imports",
        files={"files": ("case.jpg", b"quick-validation-bytes", "image/jpeg")},
        data={"mock_ocr_text": ocr_text, "label": "quick-validate-ai-ingestion"},
    )
    if response.status_code != 202:
        raise RuntimeError(f"Import endpoint rejected the upload: HTTP {response.status_code}")
    payload = response.json()
    item = payload["items"][0]
    return {
        "terminal_status": payload["status"],
        "patient_id": item["patient_id"],
        "encounter_id": item["encounter_id"],
        "document_id": item["document_id"],
        "batch_counts": {
            "accepted": payload["accepted_items"],
            "unresolved": payload["unresolved_items"],
            "failed": payload["failed_items"],
        },
    }


def _run_fallback_baseline(client, ocr_text: str) -> dict[str, Any]:
    """Same frozen OCR through the preserved deterministic parsers (no AI)."""

    settings = get_settings()
    original = settings.ai_extraction_enabled
    settings.ai_extraction_enabled = False
    try:
        return _run_case(client, ocr_text)
    finally:
        settings.ai_extraction_enabled = original


def main() -> int:
    args = build_parser().parse_args()
    _validate_case_ids(args.case_id)
    run_root = (RUNS_ROOT / args.run_id).resolve(strict=True)
    if RUNS_ROOT not in run_root.parents:
        raise ValueError(f"Run id must select a directory under runs/: {args.run_id}")

    entries = _fixture_keys(args.fixture_registry)
    expected = _load_expected_decisions(args.manifest, args.case_id)

    from app.db.base import Base
    from app.db.session import engine
    from app.main import app
    from fastapi.testclient import TestClient

    Base.metadata.create_all(engine)

    results: list[dict[str, Any]] = []
    with TestClient(app) as client:
        for case_id in args.case_id:
            ocr_text = _read_frozen_ocr(run_root, case_id)
            fixture_key = hashlib.sha256(evidence_key(ocr_text).encode("utf-8")).hexdigest()
            fixture_present = fixture_key in entries
            record: dict[str, Any] = {
                "case_id": case_id,
                "fixture_present": fixture_present,
                "expected_ingestion_decision": expected.get(case_id),
            }
            if fixture_present:
                outcome = _run_case(client, ocr_text)
                record.update(outcome)
                record["agrees_with_expected"] = (
                    expected.get(case_id) is None
                    or expected[case_id] is None
                    or (
                        (outcome["terminal_status"] == "ACCEPTED")
                        == (expected[case_id] == "AUTO_ACCEPT_IF_SAFE")
                    )
                )
            else:
                record.update(
                    {
                        "terminal_status": "SKIP",
                        "skip_reason": (
                            "NO_AI_FIXTURE: no mock extraction fixture matches this OCR "
                            "text and no live AI provider is approved (OD-010); nothing "
                            "was fabricated"
                        ),
                    }
                )
                if args.fallback_baseline:
                    record["fallback_baseline"] = _run_fallback_baseline(client, ocr_text)
            results.append(record)
            print(
                f"{case_id}: fixture={fixture_present} "
                f"status={record['terminal_status']}"
                + (
                    f" reason={record.get('skip_reason')}"
                    if record["terminal_status"] == "SKIP"
                    else ""
                )
                + (
                    f" fallback={record['fallback_baseline']['terminal_status']}"
                    if "fallback_baseline" in record
                    else ""
                )
            )

    accepted = sum(1 for r in results if r["terminal_status"] == "ACCEPTED")
    unresolved = sum(1 for r in results if r["terminal_status"] == "UNRESOLVED")
    skipped = sum(1 for r in results if r["terminal_status"] == "SKIP")
    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": args.run_id,
        "case_ids": args.case_id,
        "extraction_source": "mock_fixture" if skipped < len(results) else "none",
        "counts": {
            "accepted": accepted,
            "unresolved": unresolved,
            "skipped_no_fixture": skipped,
        },
        "disclaimer": (
            "Fixture-driven plumbing validation of the Phase 6 AI-primary chain. "
            "This is not an AI accuracy measurement. The eight zero-error usage "
            "gates require a real approved provider plus Gold comparison."
        ),
        "cases": results,
    }

    output_path = args.output or (
        PROJECT_ROOT
        / "outputs"
        / f"quick_validate_ai_ingestion_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    write_json_atomic(output_path, summary)
    print(f"Redacted report written to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
