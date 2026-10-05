"""Read operational backup evidence; never verify or mutate clinical files."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.core.config import get_settings
from app.core.security import CurrentUser

router = APIRouter(tags=["backup-status"])
_MAX_LISTED_BACKUPS = 20
_MAX_SCAN_ENTRIES = 200
_MAX_JSON_BYTES = 8 * 1024 * 1024
_STARTED = "production_isolated_restore_drill_started"
_PASSED = "production_isolated_restore_drill_passed"
_FAILED = "production_isolated_restore_drill_failed"


def _linked(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if _linked(path) or _linked(path.parent) or not path.is_file():
            return None
        with path.open("rb") as stream:
            raw = stream.read(_MAX_JSON_BYTES + 1)
        if len(raw) > _MAX_JSON_BYTES:
            return None
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _restore_records(root: Path, directory: Path, manifest: dict[str, Any]):
    """Keep legacy evidence readable; new records are hash-bound sidecars.

    Started/phase records are kept alongside passed/failed: a drill that
    began but never ended must stay visible (结果待核对), never silently
    read as "never drilled".
    """
    legacy = _read_json(directory / "restore_verification.json")
    candidates = [legacy] if legacy else []
    records_root = root / ".restore-verifications"
    record_dir = records_root / directory.name
    complete = True
    if not _linked(records_root) and not _linked(record_dir) and record_dir.is_dir():
        try:
            manifest_hash = hashlib.sha256(
                (directory / "backup_manifest.json").read_bytes()
            ).hexdigest()
            for index, path in enumerate(record_dir.iterdir()):
                if index >= _MAX_SCAN_ENTRIES:
                    complete = False
                    break
                record = _read_json(path) if path.suffix == ".json" else None
                if record and record.get("manifest_sha256") == manifest_hash:
                    candidates.append(record)
        except OSError:
            complete = False
    return [
        record for record in candidates
        if record.get("backup_id") == manifest["backup_id"]
        and record.get("environment") == manifest.get("environment")
        and record.get("status") in (_STARTED, _PASSED, _FAILED)
        and _time(record.get("verified_at")) is not None
    ], complete


def _drill_runs(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold one backup's records into drill RUNS, newest attempt first.

    Records sharing a ``run_id`` (schema 1.2: start / phase / end) form ONE
    run. A run with no end record is ``pending`` — 结果待核对: the drill may
    still be running or was interrupted; it is NEVER auto-judged either way.
    Legacy records (no run_id) each stand alone: a passed/failed record is a
    finished run, a started record is an unlinkable pending run.
    """
    runs: dict[str, dict[str, Any]] = {}
    legacy: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda row: _time(row["verified_at"])):
        status = record["status"]
        at = _time(record["verified_at"])
        at_iso = at.isoformat() if at else None
        stage = record.get("stage") if isinstance(record.get("stage"), str) else None
        error_type = (
            record.get("error_type") if isinstance(record.get("error_type"), str) else None
        )
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            legacy.append({
                "run_id": None,
                "status": "pending" if status == _STARTED else (
                    "passed" if status == _PASSED else "failed"
                ),
                "started_at": at_iso if status == _STARTED else None,
                "ended_at": None if status == _STARTED else at_iso,
                "last_stage": stage,
                "error_type": error_type if status == _FAILED else None,
            })
            continue
        run = runs.setdefault(run_id, {
            "run_id": run_id,
            "status": "pending",
            "started_at": None,
            "ended_at": None,
            "last_stage": None,
            "error_type": None,
        })
        if status == _STARTED:
            if run["started_at"] is None:
                run["started_at"] = at_iso
            if stage and stage != "start":
                run["last_stage"] = stage
        else:
            run["status"] = "passed" if status == _PASSED else "failed"
            run["ended_at"] = at_iso
            if status == _FAILED and error_type:
                run["error_type"] = error_type
            if stage and stage != "end":
                run["last_stage"] = stage
    ordered = [*runs.values(), *legacy]
    ordered.sort(
        key=lambda row: _time(row["ended_at"] or row["started_at"] or ""), reverse=True
    )
    return ordered


@router.get("/backup-status")
def backup_status(user: CurrentUser) -> dict[str, Any]:
    root_value = get_settings().backup_manifest_dir.strip()
    response: dict[str, Any] = {
        "configured": bool(root_value),
        "directory_accessible": False,
        "scan_complete": True,
        "backups": [],
        "latest_backup": None,
        "latest_restore_attempt": None,
        "latest_restore_verification": None,
    }
    if not root_value:
        return response
    root = Path(root_value)
    backups: list[dict[str, Any]] = []
    successes: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    try:
        if _linked(root) or not root.is_dir():
            return response
        # Enumerate before truncating results. An incomplete scan must never
        # claim that one candidate is globally latest. Ignore links/junctions.
        for index, directory in enumerate(root.iterdir()):
            if index >= _MAX_SCAN_ENTRIES:
                response["scan_complete"] = False
                break
            if _linked(directory) or not directory.is_dir() or directory.name.startswith("."):
                continue
            manifest = _read_json(directory / "backup_manifest.json")
            if not manifest or not isinstance(manifest.get("backup_id"), str):
                continue
            created = _time(manifest.get("created_at"))
            if created is None or manifest.get("schema_version") not in ("1.0", "1.1"):
                continue
            if not isinstance(manifest.get("files"), list):
                continue
            records, complete = _restore_records(root, directory, manifest)
            response["scan_complete"] &= complete
            # Newest ATTEMPT for this backup: passed, failed or 结果待核对.
            runs = _drill_runs(records)
            attempt = runs[0] if runs else None
            for run in runs:
                if run["status"] == "passed" and run["ended_at"]:
                    successes.append(
                        {"backup_id": manifest["backup_id"], "verified_at": run["ended_at"]}
                    )
            if attempt is not None:
                attempts.append({"backup_id": manifest["backup_id"], **attempt})
            backups.append({
                "backup_id": manifest["backup_id"],
                "created_at": created.isoformat(),
                "tier": manifest.get("tier") if isinstance(manifest.get("tier"), str) else None,
                "restore_status": attempt["status"] if attempt else None,
                "restore_run": attempt,
            })
        response["directory_accessible"] = True
    except OSError:
        response["scan_complete"] = False
        return response

    backups.sort(key=lambda row: _time(row["created_at"]), reverse=True)
    response["backups"] = backups[:_MAX_LISTED_BACKUPS]
    if response["scan_complete"]:
        if backups:
            response["latest_backup"] = {
                key: backups[0][key] for key in ("backup_id", "created_at", "tier")
            }
        # 最近尝试与最近成功分开报告：一次未结束的演练是"结果待核对"，
        # 既不冒充成功，也不掩盖更早的成功。
        if attempts:
            response["latest_restore_attempt"] = max(
                attempts, key=lambda row: _time(row["ended_at"] or row["started_at"] or "")
            )
        if successes:
            response["latest_restore_verification"] = max(
                successes, key=lambda row: _time(row["verified_at"])
            )
    return response
