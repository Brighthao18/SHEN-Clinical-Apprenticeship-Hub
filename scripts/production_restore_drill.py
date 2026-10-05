"""Phase 5.2 production isolated-restore drill.

Wraps the Phase 5.1 manifest backup with the extended Phase 5.2 checks:

1. verify the manifest backup (SHA-256 of every file);
2. restore the PostgreSQL dump into a throwaway database inside the
   production db container;
3. verify Alembic revision, full table inventory, and per-table row counts
   against the backup manifest;
4. verify the document-storage inventory (count + SHA-256 multiset) matches
   the backed-up documents tree;
5. run a synthetic PII encrypt + HMAC exact-lookup + decrypt round-trip
   against the ISOLATED restored schema (fake values only; the isolated
   database is destroyed afterwards);
6. run authenticated and anonymous API smoke checks (secrets stay inside
   the container);
7. destroy the isolated database.

Every required check is enumerated and must be exactly ``True``; an absent
section or key fails the drill (no vacuous ``all()`` over an empty set).
A verification record is persisted for EVERY run: ``started`` when the
drill begins, then ``passed`` or ``failed`` (with the failing stage) when
it ends — including exceptions and interrupts, so an interrupted drill can
never masquerade as "never drilled" and earlier successes stay on disk.

Output is counts/booleans only: no PII, no secrets, no key material.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from clinical_backup import (
    IDENTIFIER,
    MANIFEST_NAME,
    backup_profile,
    database_metadata,
    run_compose,
    safe_backup_directory,
    sha256_file,
    verify_backup,
)

STATUS_STARTED = "production_isolated_restore_drill_started"
STATUS_PASSED = "production_isolated_restore_drill_passed"
STATUS_FAILED = "production_isolated_restore_drill_failed"
# 1.2: every record of one drill shares a run_id (start / phase records /
# end), so an interrupted run is visible as 结果待核对 instead of being
# indistinguishable from "never drilled". Failure records carry CONTROLLED
# codes only (stage + exception class name) — never str(exc), which could
# embed paths or sensitive content.
RECORD_SCHEMA_VERSION = "1.2"

KEY_CLINICAL_TABLES = (
    "patients",
    "patient_identities",
    "encounters",
    "prescriptions",
    "prescription_items",
    "documents",
    "hospital_drug_specifications",
    "drug_dictionary",
    "unresolved_ingestions",
    "review_items",
)

# The exact checks the container-side script must report, each required to
# be present and exactly True. A missing key or empty section fails closed.
REQUIRED_APP_SMOKE_CHECKS = (
    "health_ok",
    "patients_authorized",
    "dashboard_authorized",
    "unresolved_queue_authorized",
    "documents_anonymous_rejected",
    "login_rejects_wrong_password",
)
REQUIRED_PII_ROUNDTRIP_CHECKS = (
    "hmac_exact_lookup_found",
    "ciphertext_decrypts_to_original",
    "stored_value_is_not_plaintext",
)


def persist_verification(backup_dir: Path, record: dict[str, Any]) -> Path:
    """Append operational evidence OUTSIDE the immutable backup inventory."""
    record_dir = backup_dir.parent / ".restore-verifications" / backup_dir.name
    for candidate in (record_dir.parent, record_dir):
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError("Restore record directory must not be a link")
    record_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        **record,
        "manifest_sha256": sha256_file(backup_dir / MANIFEST_NAME),
    }
    record_path = record_dir / f"{uuid.uuid4().hex}.json"
    # A partial write is ignored by the read-only status endpoint. Unique
    # exclusive files preserve every attempt, including earlier successes.
    with record_path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
    return record_path


def build_drill_record(
    *,
    run_id: str,
    environment: str,
    status: str,
    stage: str,
    verified_at: str,
    backup_id: str | None = None,
    error_type: str | None = None,
    isolated_database_removed: bool | None = None,
    failed_checks: list[str] | None = None,
) -> dict[str, Any]:
    """One drill record: CONTROLLED fields only.

    `stage` and `error_type` are fixed code identifiers (stage names and the
    exception class name) — never free text, so no path or secret can leak
    into the operational evidence.
    """

    payload: dict[str, Any] = {
        "schema_version": RECORD_SCHEMA_VERSION,
        "verified_at": verified_at,
        "run_id": run_id,
        "environment": environment,
        "status": status,
        "stage": stage,
    }
    if backup_id is not None:
        payload["backup_id"] = backup_id
    if error_type is not None:
        payload["error_type"] = error_type
    if isolated_database_removed is not None:
        payload["isolated_database_removed"] = isolated_database_removed
    if failed_checks is not None:
        payload["failed_checks"] = failed_checks
    return payload


def _section_checks(section: Any, required: tuple[str, ...]) -> dict[str, bool]:
    values = section if isinstance(section, dict) else {}
    return {name: values.get(name) is True for name in required}


def evaluate_drill_checks(
    *,
    restored: dict[str, Any],
    expected: dict[str, Any],
    document_parity: dict[str, Any],
    key_table_rows: dict[str, dict[str, Any]],
    container_checks: dict[str, Any],
) -> dict[str, bool]:
    """Name every required check and require each to be exactly True.

    Returns the flat named-check map; the drill passes only when
    ``all(checks.values())`` holds over this NON-empty, fully enumerated
    set. Missing sections/keys evaluate to False instead of being skipped.
    """

    checks: dict[str, bool] = {
        "alembic_revision_matches": restored.get("alembic_revision")
        == expected.get("alembic_revision"),
        "table_row_counts_equal": restored.get("table_counts")
        == expected.get("table_counts"),
        "document_sha256_multiset_equal": document_parity.get("sha256_multiset_equal")
        is True,
        "manifest_document_count_matches": document_parity.get(
            "manifest_document_file_count"
        )
        == document_parity.get("backed_up_document_files"),
        # A manifest naming NONE of the key clinical tables is evidence of a
        # broken manifest, not of an empty database.
        "key_clinical_tables_present": bool(key_table_rows),
    }
    for table, counts in key_table_rows.items():
        checks[f"key_table_rows.{table}"] = counts.get("production") == counts.get(
            "restored"
        )
    for name, value in _section_checks(
        container_checks.get("app_smoke"), REQUIRED_APP_SMOKE_CHECKS
    ).items():
        checks[f"app_smoke.{name}"] = value
    for name, value in _section_checks(
        container_checks.get("isolated_pii_roundtrip"),
        REQUIRED_PII_ROUNDTRIP_CHECKS,
    ).items():
        checks[f"isolated_pii_roundtrip.{name}"] = value
    return checks


def _run_container_checks(profile, isolated_database: str | None) -> dict[str, Any]:
    command = ["exec", "-T"]
    if isolated_database:
        command += ["-e", f"DRILL_DATABASE={isolated_database}"]
    command += ["api", "python", "-"]
    result = subprocess.run(
        ["docker", "compose", *profile.compose_options, *command],
        stdin=(PROJECT_ROOT / "scripts" / "drill_container_checks.py").open("rb"),
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"container drill checks failed: {result.stderr[-500:]}")
    return json.loads(result.stdout.strip().splitlines()[-1])


def run_drill(raw_backup_dir: Path, environment: str = "production") -> dict[str, Any]:
    profile = backup_profile(environment)
    backup_dir = safe_backup_directory(raw_backup_dir, profile, must_exist=True)
    run_id = uuid.uuid4().hex
    stage = "start"
    backup_id: str | None = None
    isolated_database_removed = False

    def record(status: str, **extra: Any) -> None:
        # Every record of this run shares the run_id: start, phase markers and
        # the end record are one traceable drill, whatever interrupts it.
        persist_verification(
            backup_dir,
            build_drill_record(
                run_id=run_id,
                environment=environment,
                status=status,
                stage=stage,
                verified_at=datetime.now(UTC).isoformat(),
                backup_id=backup_id,
                **extra,
            ),
        )

    try:
        verification = verify_backup(backup_dir, environment=environment)
        backup_id = verification["backup_id"]
        manifest = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
        user = str(manifest["database"]["user"])
        if not IDENTIFIER.fullmatch(user):
            raise ValueError("Unsafe database user in manifest")

        # The run becomes visible (结果待核对) BEFORE any heavy stage: an
        # interrupt from here on leaves a start/phase trace, never a silent
        # disappearance, and earlier successes stay on disk.
        record(STATUS_STARTED)

        # Document inventory parity: production storage vs backed-up tree. Apply
        # the same exclusion rules as backup's copy_regular_tree (dotfile
        # placeholders like .gitkeep are not clinical documents).
        stage = "document_inventory"
        record(STATUS_STARTED)

        def _clinical_files(root: Path) -> list[Path]:
            return sorted(
                path
                for path in root.rglob("*")
                if path.is_file()
                and path.name != ".gitkeep"
                and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".pyo"}
            )

        storage_files = _clinical_files(profile.storage_root)
        backup_documents = backup_dir / "documents"
        backed_up_files = _clinical_files(backup_documents)
        storage_hashes = sorted(sha256_file(path) for path in storage_files)
        backup_hashes = sorted(sha256_file(path) for path in backed_up_files)
        document_parity = {
            "production_document_files": len(storage_files),
            "backed_up_document_files": len(backed_up_files),
            "sha256_multiset_equal": storage_hashes == backup_hashes,
            "manifest_document_file_count": manifest.get("document_file_count"),
        }

        stage = "restore_database"
        record(STATUS_STARTED)
        isolated_database = f"shen_drill_{uuid.uuid4().hex[:12]}"
        if not IDENTIFIER.fullmatch(isolated_database):
            raise RuntimeError("Generated drill database name is unsafe")
        dump_path = backup_dir / "database" / "postgres.dump"
        created = False
        try:
            run_compose(
                profile,
                ["exec", "-T", "db", "createdb", "-U", user, isolated_database],
                operation="create isolated drill database",
            )
            created = True
            run_compose(
                profile,
                [
                    "exec",
                    "-T",
                    "db",
                    "pg_restore",
                    "-U",
                    user,
                    "-d",
                    isolated_database,
                    "--no-owner",
                    "--no-acl",
                    "--exit-on-error",
                ],
                stdin_path=dump_path,
                operation="restore isolated drill database",
            )
            restored = database_metadata(profile, isolated_database, user)
            expected = manifest["database"]
            key_table_rows = {
                table: {
                    "production": expected["table_counts"].get(table),
                    "restored": restored["table_counts"].get(table),
                }
                for table in KEY_CLINICAL_TABLES
                if table in expected["table_counts"]
            }
            stage = "container_checks"
            record(STATUS_STARTED)
            container_checks = _run_container_checks(profile, isolated_database)
        finally:
            if created:
                run_compose(
                    profile,
                    [
                        "exec",
                        "-T",
                        "db",
                        "dropdb",
                        "-U",
                        user,
                        "--if-exists",
                        "--force",
                        isolated_database,
                    ],
                    operation="remove isolated drill database",
                )
                isolated_database_removed = True

        stage = "evaluate_checks"
        record(STATUS_STARTED)
        checks = evaluate_drill_checks(
            restored=restored,
            expected=expected,
            document_parity=document_parity,
            key_table_rows=key_table_rows,
            container_checks=container_checks,
        )
        checks_pass = all(checks.values())
        result = {
            "status": STATUS_PASSED if checks_pass else STATUS_FAILED,
            "run_id": run_id,
            "backup_id": verification["backup_id"],
            "backup_file_count": verification["file_count"],
            "alembic_revision": verification["alembic_revision"],
            "table_count": len(restored["table_counts"]),
            "required_checks": checks,
            "checks_passed": checks_pass,
            "key_clinical_table_rows": key_table_rows,
            "document_inventory": document_parity,
            "container_checks": container_checks,
            "isolated_database_removed": isolated_database_removed,
        }
        # Never add files inside backup_dir: strict inventory verification would
        # reject that backup on the next verify/restore/restic invocation.
        stage = "end"
        record(
            result["status"],
            isolated_database_removed=isolated_database_removed,
            failed_checks=(
                [name for name, ok in checks.items() if not ok] if not checks_pass else []
            ),
        )
        return result
    except BaseException as exc:
        # Started/phases/failure are all persisted under the same run_id: an
        # interrupted drill stays 结果待核对 (never judged), and the failure
        # record carries controlled codes only — stage + exception class.
        try:
            record(
                STATUS_FAILED,
                error_type=type(exc).__name__,
                isolated_database_removed=isolated_database_removed,
            )
        except OSError as record_error:
            print(
                f"warning: could not persist failure record: {record_error}",
                file=sys.stderr,
            )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 5.2 production restore drill")
    parser.add_argument("--backup-dir", type=Path, required=True)
    parser.add_argument("--environment", default="production")
    args = parser.parse_args()
    result = run_drill(args.backup_dir, args.environment)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == STATUS_PASSED else 1


if __name__ == "__main__":
    raise SystemExit(main())
