"""Phase 6.1 — Real AI Extractor Validation on frozen CASE001-CASE005.

The same harness also validates later frozen runs (e.g. expansion cases):
any explicitly selected case ids with a frozen normalized_ocr.txt and a
gold/<case>.json file work unchanged; --verified-dictionary-json seeds
user-verified dictionary candidates into the isolated engine.

Runs ONLY: ClinicalAIExtractor (live provider) -> Validator -> Resolution,
against the already-frozen MinerU OCR text. No MinerU resubmission, no Gold
modification, no new cases, no feature work.

What it produces (private, under clinical_golden/runs/<output-run-id>/):
- per case: raw extraction payload(s), pipeline terminal result, focused
  Gold comparison (field-level), stability comparison for CASE001-003.
And a redacted summary in outputs/ (counts and per-field match flags only;
no names, MRNs, or clinical values).

Critical comparison axes (per user instruction): patient/MRN, encounter
date, item count, canonical drug, processing, explicit package dose,
package count, calculated per-decoction dose, missing/null discipline,
evidence grounding, terminal decision. Raw drug-string wording and array
order are explicitly NOT counted as clinical errors.

Usage (provider config comes from environment, never CLI history):
    AI_PROVIDER_API_KEY=... \
    python scripts/run_ai_extractor_validation.py \
        --source-run-id run-20260829T094035Z-33bdb030 \
        --case-id CASE001 ... --case-id CASE005
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
API_ROOT = PROJECT_ROOT / "apps" / "api"
sys.path.insert(0, str(API_ROOT))

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///:memory:"
os.environ["TASKS_EAGER"] = "true"
os.environ["OCR_PROVIDER"] = "mock"
os.environ["AUTH_DISABLED"] = "true"
os.environ["STORAGE_ROOT"] = str(PROJECT_ROOT / "work" / "phase61-validation-storage")

PRIVATE_ROOT = (PROJECT_ROOT / "private_data" / "clinical_golden").resolve()
RUNS_ROOT = PRIVATE_ROOT / "runs"

from app.core.config import get_settings
from app.services.ai.extractor import (
    ClinicalExtractionRejected,
    evidence_key,
    extraction_to_parsed_document,
    validate_clinical_extraction,
)
from app.services.calibration import write_json_atomic

STABILITY_CASES = ("CASE001", "CASE002", "CASE003")

# User-verified dictionary candidates seeded into the isolated engine
# (loaded in main() from --verified-dictionary-json; empty by default).
_VERIFIED_DICTIONARY: list[dict[str, Any]] = []


def _load_verified_dictionary(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise SystemExit("--verified-dictionary-json must contain an entries list")
    cleaned: list[dict[str, Any]] = []
    for entry in entries:
        raw = str(entry.get("raw_name") or "").strip()
        canonical = str(entry.get("canonical_name") or "").strip()
        if not raw or not canonical:
            raise SystemExit(
                "verified dictionary entries require raw_name and canonical_name"
            )
        cleaned.append(
            {
                "raw_name": raw,
                "canonical_name": canonical,
                "processing_method": str(entry.get("processing_method") or "").strip() or None,
            }
        )
    return cleaned


def _seed_verified_dictionary() -> int:
    """Mirror user-verified candidates into the isolated engine.

    Resolution path 1 (candidate_raw_name exact match on VERIFIED entries)
    then behaves exactly as it does against the app database. Reference
    master data only: no patient/encounter/prescription rows are touched.
    """

    if not _VERIFIED_DICTIONARY:
        return 0
    from app.core.config import get_settings
    from app.db.base import utcnow
    from app.db.session import SessionLocal
    from app.models.domain import DrugDictionary
    from sqlalchemy import select

    seeded = 0
    with SessionLocal() as session:
        for entry in _VERIFIED_DICTIONARY:
            exists = session.scalar(
                select(DrugDictionary).where(
                    DrugDictionary.candidate_raw_name == entry["raw_name"]
                )
            )
            if exists is not None:
                continue
            session.add(
                DrugDictionary(
                    canonical_name=entry["canonical_name"],
                    processing_method=entry["processing_method"],
                    aliases=[entry["raw_name"]],
                    hospital_display_names=[entry["raw_name"]],
                    common_ocr_errors=[],
                    dictionary_version=get_settings().drug_dictionary_version,
                    effective_from=utcnow().date(),
                    source="user_verified_dictionary_export",
                    status="VERIFIED",
                    candidate_raw_name=entry["raw_name"],
                    usage_count=0,
                    source_document_ids=[],
                )
            )
            seeded += 1
        session.commit()
    return seeded

# Error taxonomy (mutually exclusive first cause, per user instruction).
ERROR_TAXONOMY = (
    "AI_EXTRACTION_ERROR",
    "EVIDENCE_MATCHING_ERROR",
    "DRUG_DICTIONARY_COVERAGE",
    "VALIDATION_POLICY_ERROR",
    "OCR_SOURCE_ERROR",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Phase 6.1 real-AI extraction validation (frozen cases only)"
    )
    parser.add_argument("--source-run-id", required=True, help="Frozen run with normalized_ocr.txt")
    parser.add_argument("--case-id", action="append", required=True)
    parser.add_argument(
        "--stability-case",
        action="append",
        dest="stability_cases",
        default=None,
        help="Cases to run twice (default: CASE001 CASE002 CASE003)",
    )
    parser.add_argument(
        "--no-stability",
        action="store_true",
        help="Run each selected case once (skip double-run stability checks)",
    )
    parser.add_argument(
        "--output-run-id",
        default=None,
        help="Private output run dir (default: run-<UTC timestamp>-phase61)",
    )
    parser.add_argument(
        "--provider-config-db",
        default=None,
        help=(
            "SQLAlchemy URL of the app database that holds the provider slot "
            "configured through the settings UI (e.g. the dev/production "
            "PostgreSQL). Alternative to env-based provider configuration."
        ),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help=(
            "Optional env file for PII secrets when reading the provider slot from "
            "the production database (values never printed). Never loaded by default: "
            "this run must stay on the isolated test profile."
        ),
    )
    parser.add_argument(
        "--verified-dictionary-json",
        type=Path,
        default=None,
        help=(
            "Optional JSON {entries:[{raw_name, canonical_name, processing_method}]} "
            "of user-verified dictionary candidates exported from the app database. "
            "Seeded into the isolated engine so resolution exercises the real "
            "verified dictionary. Reference data only; no clinical rows."
        ),
    )
    return parser


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        if name.strip() not in os.environ:
            os.environ[name.strip()] = value


def _apply_provider_config_db(database_url: str) -> None:
    """Read the UI-configured provider slot from the app database.

    Only the provider parameters are read; validation/pipeline data still
    runs in the isolated in-memory database. The API key never prints.
    """

    from app.models.domain import ProviderSetting
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            row = session.query(ProviderSetting).filter(ProviderSetting.slot == "clinical_extraction").first()
    finally:
        engine.dispose()
    if row is None:
        raise SystemExit(
            "No provider slot found in the given database. Configure it first via "
            "the settings UI (/settings) or pass AI_PROVIDER_API_KEY via env."
        )
    if not row.enabled or not row.confirmed_identifiable_upload:
        raise SystemExit("The configured provider slot is disabled or unconfirmed.")

    settings = get_settings()
    settings.ai_provider_base_url = row.base_url
    settings.ai_provider_model = row.model
    # EncryptedText decrypts on attribute access; must match the writer's
    # PII secret (pass --env-file when validating against the production DB).
    settings.ai_provider_api_key = row.api_key_encrypted
    settings.ai_extraction_allow_identifiable_input = True
    settings.ai_extraction_provider = row.adapter
    print(f"provider slot: {row.adapter} model={row.model} base_url={row.base_url}")


def _read_frozen_ocr(source_run_root: Path, case_id: str) -> str:
    ocr_path = (source_run_root / case_id / "normalized_ocr.txt").resolve(strict=True)
    if RUNS_ROOT not in ocr_path.parents:
        raise ValueError(f"Refusing to read outside frozen runs: {ocr_path}")
    return ocr_path.read_text(encoding="utf-8")


def _load_gold(case_id: str) -> dict[str, Any]:
    gold_path = PRIVATE_ROOT / "gold" / f"{case_id}.json"
    return json.loads(gold_path.read_text(encoding="utf-8"))


def _decimal(value: Any) -> str | None:
    """Compare dose/package values numerically.

    DB Numeric columns serialize as "10.000" while Gold annotations carry
    "10"; string equality reported false field mismatches (expansion-7 run).
    """

    if value is None:
        return None
    try:
        normalized = Decimal(str(value)).normalize()
    except (ValueError, ArithmeticError):
        return str(value)
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal(1)))
    return str(normalized)


def _live_provider_required(settings) -> None:
    if settings.ai_extraction_provider != "live_openai_compatible":
        raise SystemExit(
            "Refusing to run: AI_EXTRACTION_PROVIDER must be 'live_openai_compatible' "
            "for Phase 6.1 (mock fixtures are not a real-AI measurement)."
        )


class _ReplayProvider:
    """Replays one captured live-AI payload through the real pipeline.

    Guarantees 1 real AI call per run: the Validator -> Resolution stages
    inside PipelineService consume exactly the payload captured above, not a
    second provider call. Harness plumbing only; no architecture change.
    """

    name = "replay_live"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def extract(self, ocr_text: str) -> dict[str, Any]:
        return self._payload

    def verify(
        self, ocr_text: str, payload: dict[str, Any], blocking_issue_codes: list[str]
    ) -> dict[str, Any] | None:
        return None


def _run_pipeline_once(ocr_text: str, replay_payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, list[str]]:
    """Run Validator -> Resolution in PipelineService on a captured payload.

    Returns (terminal report, committed prescription payload or None, unresolved reasons).
    """

    import app.services.pipeline as pipeline_module
    from app.db.base import Base
    from app.db.session import SessionLocal, engine
    from app.main import app
    from fastapi.testclient import TestClient

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    # In-memory SQLite: drop/create on the pooled connection keeps each run
    # fully isolated (no cross-case patient matching state).
    engine.dispose()
    Base.metadata.create_all(engine)
    _seed_verified_dictionary()

    original = pipeline_module.get_clinical_extraction_provider
    pipeline_module.get_clinical_extraction_provider = lambda: _ReplayProvider(replay_payload)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/v1/imports",
                files={"files": ("case.jpg", b"phase61-validation", "image/jpeg")},
                data={"mock_ocr_text": ocr_text, "label": "phase61-ai-extractor-validation"},
            )
            if response.status_code != 202:
                raise RuntimeError(f"import rejected: HTTP {response.status_code}: {response.text}")
            batch = response.json()
            item = batch["items"][0]
    finally:
        pipeline_module.get_clinical_extraction_provider = original

    terminal = {
        "terminal_status": batch["status"],
        "patient_id": item["patient_id"],
        "encounter_id": item["encounter_id"],
        "document_id": item["document_id"],
    }
    prescription: dict[str, Any] | None = None
    reasons: list[str] = []
    document_uuid = uuid.UUID(item["document_id"])
    with SessionLocal() as session:
        from app.models.domain import Document, PrescriptionItem, UnresolvedIngestion
        from sqlalchemy import select

        document = session.get(Document, document_uuid)
        if document is not None:
            terminal["parser_template"] = document.parser_template
            terminal["structured_snapshot"] = document.structured_snapshot
            terminal["failure_code"] = document.failure_code
        ledger = session.scalar(
            select(UnresolvedIngestion).where(UnresolvedIngestion.document_id == document_uuid)
        )
        if ledger is not None:
            reasons = [ledger.failure_reason_code]
        if item["encounter_id"] and terminal["terminal_status"] == "ACCEPTED":
            items = session.scalars(
                select(PrescriptionItem).order_by(PrescriptionItem.line_number)
            ).all()
            prescription = {
                "items": [
                    {
                        "raw_drug_name": row.raw_drug_name,
                        "canonical_drug_name": row.canonical_drug_name,
                        "processing_method": row.processing_method,
                        "explicit_package_dose_g": _decimal(row.explicit_package_dose_g),
                        "package_count": _decimal(row.package_count),
                        "calculated_dose_g": _decimal(row.calculated_dose_g),
                        "dose_scope": row.dose_scope.value,
                    }
                    for row in items
                ]
            }
    return terminal, prescription, reasons


def _critical_signature(
    extraction: dict[str, Any], prescription: dict[str, Any] | None
) -> dict[str, Any]:
    """Order-independent critical-field signature of one resolved run."""

    patient = extraction.get("patient", {})
    encounter = extraction.get("encounter", {})
    presc = extraction.get("prescription", {})
    item_keys = []
    for item in presc.get("items", []):
        item_keys.append(
            {
                "raw_drug_name": item.get("raw_drug_name"),
                "processing_method": item.get("processing_method"),
                "explicit_package_dose_g": _decimal(item.get("explicit_package_dose_g")),
                "package_count": _decimal(item.get("package_count")),
                "package_unit": item.get("package_unit"),
            }
        )
    item_keys.sort(key=lambda entry: json.dumps(entry, ensure_ascii=False, sort_keys=True))
    resolved_keys = []
    if prescription is not None:
        for item in prescription["items"]:
            resolved_keys.append(
                {
                    "canonical_drug_name": item["canonical_drug_name"],
                    "processing_method": item["processing_method"],
                    "explicit_package_dose_g": item["explicit_package_dose_g"],
                    "package_count": item["package_count"],
                    "calculated_dose_g": item["calculated_dose_g"],
                }
            )
        resolved_keys.sort(key=lambda entry: json.dumps(entry, ensure_ascii=False, sort_keys=True))
    return {
        "real_name": patient.get("real_name"),
        "medical_record_number": patient.get("medical_record_number"),
        "sex": patient.get("sex"),
        "age": patient.get("age"),
        "encounter_date": encounter.get("encounter_date"),
        "hospital": encounter.get("hospital"),
        "total_decoctions": presc.get("total_decoctions"),
        "item_count": len(presc.get("items", [])),
        "item_keys": item_keys,
        "resolved_item_keys": resolved_keys,
    }


def _compare_critical(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    differences = []
    for field in (
        "real_name",
        "medical_record_number",
        "sex",
        "age",
        "encounter_date",
        "hospital",
        "total_decoctions",
        "item_count",
    ):
        if a.get(field) != b.get(field):
            differences.append(field)
    if a.get("item_keys") != b.get("item_keys"):
        differences.append("item_keys")
    if a.get("resolved_item_keys") != b.get("resolved_item_keys"):
        differences.append("resolved_item_keys")
    return differences


def _hallucinated_fields(extraction: dict[str, Any], ocr_text: str) -> list[str]:
    """Non-null string fields whose content is not traceable to the OCR text."""

    haystack = evidence_key(ocr_text)
    suspects: list[str] = []

    def _trace(field: str, value: Any) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        for fragment in re_split_fragments(value):
            if fragment and evidence_key(fragment) not in haystack:
                suspects.append(f"{field}:{fragment[:24]}")

    patient = extraction.get("patient", {})
    encounter = extraction.get("encounter", {})
    for field, value in patient.items():
        _trace(f"patient.{field}", value)
    for field, value in encounter.items():
        _trace(f"encounter.{field}", value)
    for index, symptom in enumerate(extraction.get("symptoms", [])):
        _trace(f"symptoms[{index}].text", symptom.get("text"))
    for index, diagnosis in enumerate(extraction.get("diagnoses", [])):
        _trace(f"diagnoses[{index}].text", diagnosis.get("text"))
    presc = extraction.get("prescription", {})
    for index, item in enumerate(presc.get("items", [])):
        _trace(f"prescription.items[{index}].raw_drug_name", item.get("raw_drug_name"))
        _trace(f"prescription.items[{index}].processing_method", item.get("processing_method"))
    return suspects


def re_split_fragments(value: str) -> list[str]:
    import re

    return [fragment.strip() for fragment in re.split(r"[，,、；;。：:（）()\s]+", value) if fragment.strip()]


def _align_items(
    gold_items: list[dict[str, Any]], ai_items: list[dict[str, Any]]
) -> dict[str, Any]:
    """Semantic alignment on (canonical, processing); order/wording ignored.

    canonical fallback: gold raw_drug_name stem match against AI raw name is
    NOT used (per instruction, wording differences are not clinical errors);
    alignment uses canonical name from Gold vs resolved AI canonical name,
    falling back to exact raw-name equality only when one side lacks a
    canonical value.
    """

    def _key_canonical(item: dict[str, Any], resolved: bool) -> str:
        name = item.get("canonical_drug_name") or item.get("raw_drug_name") or ""
        processing = item.get("processing_method") or ""
        return f"{name}|{processing}"

    remaining = list(range(len(ai_items)))
    matched: list[dict[str, Any]] = []
    for gold_index, gold_item in enumerate(gold_items):
        gold_key = _key_canonical(gold_item, resolved=False)
        found = None
        for ai_index in remaining:
            ai_entry = ai_items[ai_index]
            ai_key = _key_canonical(ai_entry, resolved=True)
            if ai_key == gold_key:
                found = ai_index
                break
        if found is None:
            matched.append(
                {
                    "gold_line": gold_item.get("line_number", gold_index + 1),
                    "canonical": gold_item.get("canonical_drug_name"),
                    "status": "MISSING_IN_AI",
                }
            )
            continue
        remaining.remove(found)
        ai_entry = ai_items[found]
        field_diffs = []
        for field in (
            "explicit_package_dose_g",
            "package_count",
            "calculated_dose_g",
            "processing_method",
        ):
            gold_value = _decimal(gold_item.get(field))
            ai_value = _decimal(ai_entry.get(field))
            if field == "processing_method":
                gold_value = gold_item.get(field)
                ai_value = ai_entry.get(field)
            if gold_value != ai_value:
                field_diffs.append(field)
        if gold_item.get("canonical_drug_name") != ai_entry.get("canonical_drug_name"):
            field_diffs.append("canonical_drug_name")
        matched.append(
            {
                "gold_line": gold_item.get("line_number", gold_index + 1),
                "canonical": gold_item.get("canonical_drug_name"),
                "status": "MATCH" if not field_diffs else "FIELD_MISMATCH",
                "field_diffs": field_diffs,
            }
        )
    unexpected = [
        {
            "canonical": ai_items[ai_index].get("canonical_drug_name"),
            "raw": ai_items[ai_index].get("raw_drug_name"),
        }
        for ai_index in remaining
    ]
    return {
        "gold_item_count": len(gold_items),
        "ai_item_count": len(ai_items),
        "matched": matched,
        "unexpected_in_ai": unexpected,
    }


def main() -> int:
    args = build_parser().parse_args()
    if args.env_file and args.env_file.exists():
        _load_env_file(args.env_file)
    get_settings.cache_clear()
    settings = get_settings()
    if args.provider_config_db:
        _apply_provider_config_db(args.provider_config_db)
    _live_provider_required(settings)

    case_ids = args.case_id
    stability_cases = () if args.no_stability else (
        tuple(args.stability_cases) if args.stability_cases else STABILITY_CASES
    )
    for case_id in list(case_ids) + list(stability_cases):
        if case_id not in case_ids:
            raise SystemExit(f"Stability case {case_id} must also be selected via --case-id")
        if not case_id.startswith("CASE") or not case_id[4:].isdigit():
            raise SystemExit(f"Bad case id: {case_id}")

    source_run_root = (RUNS_ROOT / args.source_run_id).resolve(strict=True)
    if RUNS_ROOT not in source_run_root.parents:
        raise SystemExit("source run id must select a directory under runs/")

    global _VERIFIED_DICTIONARY
    _VERIFIED_DICTIONARY = _load_verified_dictionary(args.verified_dictionary_json)
    if _VERIFIED_DICTIONARY:
        print(f"verified dictionary candidates to seed: {len(_VERIFIED_DICTIONARY)}")

    output_run_id = args.output_run_id or (
        f"run-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-phase61"
    )
    output_root = RUNS_ROOT / output_run_id
    output_root.mkdir(parents=True, exist_ok=False)

    from app.services.ai.extractor import get_clinical_extraction_provider

    provider = get_clinical_extraction_provider()
    print(f"provider={provider.name} model={settings.ai_provider_model}")
    print(f"private artifacts: {output_root}")

    redacted_cases: list[dict[str, Any]] = []
    counters = {
        "critical_false_import_count": 0,
        "hallucinated_field_count": 0,
        "evidence_mismatch_count": 0,
        "unknown_drug_total": 0,
        "unknown_drug_ai_error": 0,
        "unknown_drug_dictionary_coverage": 0,
        "stability_mismatch_count": 0,
    }

    for case_id in case_ids:
        ocr_text = _read_frozen_ocr(source_run_root, case_id)
        gold = _load_gold(case_id)
        expected = gold["expected"]
        case_dir = output_root / case_id
        case_dir.mkdir(exist_ok=True)
        (case_dir / "normalized_ocr.source.txt").write_text(
            f"source_run={args.source_run_id}\n", encoding="utf-8"
        )

        runs: list[dict[str, Any]] = []
        run_count = 2 if case_id in stability_cases else 1
        for run_index in range(1, run_count + 1):
            run_payload: dict[str, Any] = {"run": run_index}
            try:
                # The only real AI call for this run.
                raw_extraction = provider.extract(ocr_text)
                run_payload["extraction"] = raw_extraction
                extraction = validate_clinical_extraction(raw_extraction, ocr_text)
                parsed = extraction_to_parsed_document(
                    extraction,
                    template="AI_UNIFIED",
                    parser_version=(
                        f"ai-clinical/{settings.ai_extractor_version}+{settings.ai_provider_model}"
                    ),
                )
                run_payload["converted_warnings"] = parsed.warnings
                (case_dir / f"extraction_run{run_index}.json").write_text(
                    json.dumps(raw_extraction, ensure_ascii=False, indent=1), encoding="utf-8"
                )
            except ClinicalExtractionRejected as exc:
                run_payload["rejection"] = {"issue_code": exc.issue_code, "message": str(exc)}
                # Retain the rejected raw payload for offline diagnosis; the
                # comparison below still runs against it, but without this
                # file the model output of a paid call would be unrecoverable
                # (expansion-7 CASE026 observation).
                (case_dir / f"extraction_rejected_run{run_index}.json").write_text(
                    json.dumps(
                        {
                            "issue_code": exc.issue_code,
                            "message": str(exc),
                            "payload": raw_extraction,
                        },
                        ensure_ascii=False,
                        indent=1,
                        default=str,
                    ),
                    encoding="utf-8",
                )
                terminal = {"terminal_status": "UNRESOLVED"}
                prescription = None
                reasons = [exc.issue_code]
            else:
                # Resolution through the real pipeline on the SAME payload.
                terminal, prescription, reasons = _run_pipeline_once(ocr_text, raw_extraction)
                hallucinations = _hallucinated_fields(raw_extraction, ocr_text)
                counters["hallucinated_field_count"] += len(hallucinations)
                if hallucinations:
                    (case_dir / f"hallucination_suspects_run{run_index}.json").write_text(
                        json.dumps(hallucinations, ensure_ascii=False, indent=1),
                        encoding="utf-8",
                    )
                run_payload["hallucination_suspects"] = hallucinations
                run_payload["signature"] = _critical_signature(raw_extraction, prescription)
                (case_dir / f"pipeline_result_run{run_index}.json").write_text(
                    json.dumps(
                        {"terminal": terminal, "reasons": reasons},
                        ensure_ascii=False,
                        indent=1,
                        default=str,
                    ),
                    encoding="utf-8",
                )

            runs.append(run_payload)
            if run_index == 1:
                first_run = {
                    "run_payload": run_payload,
                    "terminal": terminal,
                    "prescription": prescription,
                    "reasons": reasons,
                }

        # Stability comparison for double-run cases.
        stability: dict[str, Any] | None = None
        if run_count == 2 and "rejection" not in runs[0] and "rejection" not in runs[1]:
            differences = _compare_critical(runs[0]["signature"], runs[1]["signature"])
            stability = {"critical_field_diffs": differences, "core_structure_stable": not differences}
            if differences:
                counters["stability_mismatch_count"] += 1
        elif run_count == 2:
            stability = {
                "critical_field_diffs": ["EXTRACTION_REJECTED"],
                "core_structure_stable": False,
            }
            counters["stability_mismatch_count"] += 1
        if stability is not None:
            (case_dir / "stability.json").write_text(
                json.dumps(stability, ensure_ascii=False, indent=1), encoding="utf-8"
            )

        # Focused Gold comparison on the first run.
        comparison: dict[str, Any] = {"case_id": case_id}
        first_extraction = first_run["run_payload"].get("extraction")
        terminal = first_run["terminal"]
        prescription = first_run["prescription"]
        reasons = first_run["reasons"]
        if first_extraction is None:
            comparison.update(
                {
                    "result": "EXTRACTION_REJECTED",
                    "rejection": first_run["run_payload"].get("rejection"),
                    "error_class": "AI_EXTRACTION_ERROR",
                }
            )
        else:
            patient = first_extraction.get("patient", {})
            encounter = first_extraction.get("encounter", {})
            gold_items = expected["prescription"]["items"]
            ai_items = prescription["items"] if prescription else []
            alignment = _align_items(gold_items, ai_items)
            field_matches = {
                "real_name": patient.get("real_name")
                == expected["patient_identity"]["real_name"],
                "medical_record_number": patient.get("medical_record_number")
                == expected["patient_identity"]["medical_record_number"],
                "encounter_date": encounter.get("encounter_date")
                == expected["encounter"]["encounter_date"],
                "item_count": alignment["ai_item_count"] == alignment["gold_item_count"],
            }
            matched_clean = sum(1 for m in alignment["matched"] if m["status"] == "MATCH")
            expected_decision = gold.get("expected_ingestion_decision")
            imported = terminal.get("terminal_status") == "ACCEPTED"
            unresolved_reasons = reasons

            critical_wrong = [
                field for field, ok in field_matches.items() if not ok
            ]
            critical_wrong.extend(
                f"item:{m['gold_line']}:{','.join(m['field_diffs'])}"
                for m in alignment["matched"]
                if m["status"] == "FIELD_MISMATCH"
            )
            critical_wrong.extend(
                f"missing_item:{m['canonical']}" for m in alignment["matched"] if m["status"] == "MISSING_IN_AI"
            )
            if imported and critical_wrong:
                counters["critical_false_import_count"] += 1

            # UNKNOWN_DRUG attribution: dictionary coverage vs AI nonsense.
            unknown_drugs = [
                item
                for item in first_extraction.get("prescription", {}).get("items", [])
                if item.get("suggested_canonical_name") is None
                and item.get("raw_drug_name") not in _SEED_KNOWN_DRUGS
            ]
            dictionary_coverage = [
                item.get("raw_drug_name") for item in unknown_drugs
            ]
            counters["unknown_drug_total"] += len(unknown_drugs)
            counters["unknown_drug_dictionary_coverage"] += len(dictionary_coverage)
            comparison.update(
                {
                    "result": "IMPORTED" if imported else "UNRESOLVED",
                    "unresolved_reasons": unresolved_reasons,
                    "field_matches": field_matches,
                    "alignment": alignment,
                    "matched_clean_count": matched_clean,
                    "critical_wrong_fields": critical_wrong,
                    "unknown_drug_not_in_dictionary": dictionary_coverage,
                    "expected_decision": expected_decision,
                    "decision_agrees": (
                        imported == (expected_decision == "AUTO_ACCEPT_IF_SAFE")
                        if expected_decision
                        else None
                    ),
                    "error_class": (
                        "VALIDATION_POLICY_ERROR"
                        if imported and critical_wrong
                        else None
                    ),
                }
            )
            # Full field-level comparison is private (contains real values).
            (case_dir / "comparison_with_gold.json").write_text(
                json.dumps(
                    {
                        **comparison,
                        "gold_expected": expected,
                        "ai_resolved_prescription": prescription,
                    },
                    ensure_ascii=False,
                    indent=1,
                    default=str,
                ),
                encoding="utf-8",
            )

        (case_dir / "comparison_summary.json").write_text(
            json.dumps(comparison, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8",
        )
        redacted_cases.append(
            {
                "case_id": case_id,
                "result": comparison.get("result", "EXTRACTION_REJECTED"),
                "unresolved_reasons": comparison.get("unresolved_reasons", []),
                "gold_item_count": comparison.get("alignment", {}).get("gold_item_count"),
                "ai_item_count": comparison.get("alignment", {}).get("ai_item_count"),
                "matched_clean_count": comparison.get("matched_clean_count"),
                "critical_wrong_fields": comparison.get("critical_wrong_fields", []),
                "field_matches": comparison.get("field_matches", {}),
                "stability": stability,
            }
        )
        print(f"{case_id}: {comparison.get('result')} reasons={comparison.get('unresolved_reasons')}")

    summary = {
        "schema_version": 1,
        "phase": "6.1",
        "generated_at": datetime.now(UTC).isoformat(),
        "provider": {
            "name": "live_openai_compatible",
            "model": settings.ai_provider_model,
            "base_url": settings.ai_provider_base_url,
            "temperature": 0,
        },
        "source_run_id": args.source_run_id,
        "output_run_id": output_run_id,
        "counters": counters,
        "disclaimer": (
            "Real-AI validation run on frozen OCR. Field-level values live only in the "
            "private run directory; this summary is redacted."
        ),
        "cases": redacted_cases,
    }
    outputs_path = (
        PROJECT_ROOT
        / "outputs"
        / f"phase61_ai_extractor_validation_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    write_json_atomic(outputs_path, summary)
    print(f"redacted summary: {outputs_path}")
    return 0


# Seed dictionary keys for UNKNOWN_DRUG attribution reporting only; the real
# gate is the DrugDictionary inside the pipeline.
_SEED_KNOWN_DRUGS = {
    "鳖甲", "醋鳖甲", "白花蛇舌草", "半枝莲", "茯神", "知母", "枳壳", "柿蒂",
    "益智仁", "桑螵蛸", "红景天", "藏红曲",
}


if __name__ == "__main__":
    raise SystemExit(main())
