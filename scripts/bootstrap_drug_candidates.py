"""One-time Dictionary Bootstrap from Phase 6.1 validation artifacts.

Aggregates the raw drug names the real-AI validation extracted (AI extraction
error = 0 confirmed by Gold comparison) into deduplicated PENDING candidates
so the user can confirm them in one pass via the /dictionary page.

Candidates are reference data only: nothing is set to VERIFIED here, no
clinical rows are touched, and only explicit artifact paths are read.

Dry run (default): prints the deduplicated candidate table.
--apply: upserts into the DEV database through the running api container
(`docker compose exec api python -`), reusing the pipeline's own
upsert_candidate service. Usage counts come from the artifacts.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))
DEFAULT_RUN = PROJECT_ROOT / "private_data/clinical_golden/runs/run-20260830-phase61r2-deepseek"
DEFAULT_ARTIFACTS = [
    DEFAULT_RUN / "CASE001" / "extraction_run1.json",
    DEFAULT_RUN / "CASE002" / "extraction_run1.json",
    DEFAULT_RUN / "CASE003" / "extraction_run1.json",
    DEFAULT_RUN / "CASE005" / "extraction_run1.json",
    # CASE004 was fail-closed rejected; its diagnostic payload documents what
    # the model saw before refusing (no candidates are taken from a refusal).
]
CASE_IDS = ["CASE001", "CASE002", "CASE003", "CASE004", "CASE005"]

_CONTAINER_UPSERT = """
import json
from app.db.session import SessionLocal
from app.services.drug_candidates import upsert_candidate

payload = json.loads(r\"\"\"__PAYLOAD__\"\"\")
created = reused = 0
with SessionLocal() as session:
    for entry in payload:
        _, was_created = upsert_candidate(
            session,
            raw_name=entry["raw"],
            suggested_canonical=entry.get("suggested_canonical"),
            suggested_processing=entry.get("suggested_processing"),
            evidence_text=entry.get("evidence"),
            source_document_id=None,
        )
        if was_created:
            created += 1
        else:
            reused += 1
    session.commit()
print(json.dumps({"created": created, "reused": reused}))
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap PENDING drug candidates from Phase 6.1 artifacts"
    )
    parser.add_argument(
        "--artifact",
        action="append",
        type=Path,
        default=None,
        help="Extraction payload JSON (repeatable; default: phase61 r2 run artifacts)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Upsert into the dev database via the running api container",
    )
    return parser


def aggregate(paths: list[Path]) -> list[dict]:
    from app.services.drugs import SEED_DRUGS

    by_raw: dict[str, dict] = {}
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        case_id = path.parent.name
        for item in payload.get("prescription", {}).get("items", []):
            raw = (item.get("raw_drug_name") or "").strip()
            if not raw:
                continue
            # Mirror the pipeline: seed-resolvable names never become candidates.
            if raw in SEED_DRUGS:
                continue
            entry = by_raw.setdefault(
                raw,
                {
                    "raw": raw,
                    "suggested_canonical": None,
                    "suggested_processing": None,
                    "evidence": None,
                    "usage_count": 0,
                    "cases": [],
                },
            )
            entry["usage_count"] += 1
            if case_id not in entry["cases"]:
                entry["cases"].append(case_id)
            if entry["suggested_canonical"] is None and item.get("suggested_canonical_name"):
                entry["suggested_canonical"] = item["suggested_canonical_name"]
            if entry["suggested_processing"] is None and item.get("processing_method"):
                entry["suggested_processing"] = item["processing_method"]
            if entry["evidence"] is None and item.get("original_text"):
                entry["evidence"] = item["original_text"]
    return sorted(by_raw.values(), key=lambda entry: (-entry["usage_count"], entry["raw"]))


def main() -> int:
    args = build_parser().parse_args()
    paths = args.artifact or DEFAULT_ARTIFACTS
    for path in paths:
        if not path.exists():
            print(f"missing artifact: {path}")
    paths = [path for path in paths if path.exists()]

    candidates = aggregate(paths)
    print(f"deduplicated candidates: {len(candidates)} (from {len(paths)} artifacts)")
    for entry in candidates:
        suggestion = entry["suggested_canonical"] or "—"
        if entry["suggested_processing"]:
            suggestion += f"/{entry['suggested_processing']}"
        print(f"  {entry['raw']:<8} -> {suggestion:<12} x{entry['usage_count']}  {entry['cases']}")

    if not args.apply:
        print("\ndry run only; pass --apply to upsert into the dev database")
        return 0

    payload = json.dumps(candidates, ensure_ascii=False)
    program = _CONTAINER_UPSERT.replace("__PAYLOAD__", payload)
    result = subprocess.run(
        ["docker", "compose", "exec", "-T", "api", "python", "-"],
        cwd=PROJECT_ROOT,
        input=program,
        capture_output=True,
        text=True,
        check=True,
    )
    print(result.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
