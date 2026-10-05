from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import clinical_backup
except ModuleNotFoundError:  # Imported as scripts.restic_backup during validation.
    from scripts import clinical_backup

MANIFEST_NAME = clinical_backup.MANIFEST_NAME
PROJECT_ROOT = clinical_backup.PROJECT_ROOT

DEFAULT_PRODUCTION_CONFIG = PROJECT_ROOT / ".env.production.local"
RESTORE_TEST_ROOT = (PROJECT_ROOT / "work" / "restic_restore_verify").resolve()
LOCAL_REPOSITORY_PREFIXES = ("local:",)
REMOTE_REPOSITORY_PREFIXES = ("s3:", "sftp:", "rest:", "azure:", "gs:", "b2:", "rclone:")


@dataclass(frozen=True)
class ResticConfiguration:
    executable: Path
    repository: str
    password_file: Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Encrypt verified SHEN manifest backups with a local or external-disk restic repository."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "init", "check", "snapshots", "retention-plan"):
        command_parser = subparsers.add_parser(command)
        _add_common_arguments(command_parser)
        if command == "check":
            command_parser.add_argument(
                "--read-data", action="store_true", help="Verify all encrypted pack data."
            )
    backup_parser = subparsers.add_parser("backup")
    _add_common_arguments(backup_parser)
    backup_parser.add_argument("--backup-dir", type=Path, required=True)
    restore_parser = subparsers.add_parser("restore-verify")
    _add_common_arguments(restore_parser)
    restore_parser.add_argument("--snapshot", default="latest")
    return parser


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--environment", choices=["development", "production"], default="production"
    )
    parser.add_argument(
        "--config-file",
        type=Path,
        help="Defaults to .env.production.local for production.",
    )


def parse_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Restic config line {line_number} is invalid")
        name, value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values:
            raise ValueError(f"Restic config line {line_number} has an invalid variable name")
        stripped = value.strip()
        if len(stripped) >= 2 and stripped[0] == stripped[-1] == "'":
            stripped = stripped[1:-1].replace("\\'", "'")
        elif len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
            stripped = stripped[1:-1]
        values[name] = stripped
    return values


def load_configuration(environment: str, config_file: Path | None) -> ResticConfiguration:
    if config_file is None:
        if environment != "production":
            raise ValueError("Development restic validation requires an explicit --config-file")
        config_path = DEFAULT_PRODUCTION_CONFIG.resolve(strict=True)
    else:
        config_path = config_file.expanduser().resolve(strict=True)
    values = parse_environment(config_path)
    raw_repository = values.get("RESTIC_REPOSITORY", "")
    raw_password_file = values.get("RESTIC_PASSWORD_FILE", "")
    if not raw_repository or not raw_password_file:
        raise ValueError("RESTIC_REPOSITORY and RESTIC_PASSWORD_FILE are required")
    password_file = _resolve_path(raw_password_file).resolve(strict=True)
    if not password_file.is_file() or password_file.is_symlink():
        raise ValueError("Restic password file must be a safe regular file")
    executable = locate_restic(values.get("RESTIC_EXECUTABLE"))
    repository = _normalize_repository(raw_repository)
    return ResticConfiguration(executable, repository, password_file)


def locate_restic(configured: str | None = None) -> Path:
    candidates: list[Path] = []
    if configured:
        candidates.append(_resolve_path(configured))
    discovered = shutil.which("restic")
    if discovered:
        candidates.append(Path(discovered))
    candidates.extend(
        sorted(
            (PROJECT_ROOT / "work" / "tools" / "restic").glob("*/restic*.exe"),
            reverse=True,
        )
    )
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file() and not resolved.is_symlink():
            return resolved
    raise FileNotFoundError(
        "restic executable not found; install it or set RESTIC_EXECUTABLE to a trusted binary"
    )


def _resolve_path(raw_path: str) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate


def _normalize_repository(raw_repository: str) -> str:
    if raw_repository.startswith(REMOTE_REPOSITORY_PREFIXES):
        return raw_repository
    for prefix in LOCAL_REPOSITORY_PREFIXES:
        raw_repository = raw_repository.removeprefix(prefix)
    return str(_resolve_path(raw_repository).resolve())


def run_restic(
    configuration: ResticConfiguration,
    arguments: list[str],
    *,
    operation: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("RESTIC_PASSWORD", None)
    command = [
        str(configuration.executable),
        "--repo",
        configuration.repository,
        "--password-file",
        str(configuration.password_file),
        *arguments,
    ]
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{operation} failed with exit code {result.returncode}")
    return result


def status(configuration: ResticConfiguration) -> dict[str, Any]:
    version_result = subprocess.run(
        [str(configuration.executable), "version"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if version_result.returncode != 0:
        raise RuntimeError("Unable to execute restic")
    initialized = False
    try:
        run_restic(configuration, ["cat", "config"], operation="read restic repository")
        initialized = True
    except RuntimeError:
        initialized = False
    version_match = re.search(r"restic\s+([^\s]+)", version_result.stdout)
    return {
        "status": "restic_available",
        "restic_version": version_match.group(1) if version_match else "unknown",
        "repository_initialized": initialized,
        "password_value_printed": False,
    }


def initialize_repository(configuration: ResticConfiguration) -> dict[str, Any]:
    current = status(configuration)
    if current["repository_initialized"]:
        return {"status": "already_initialized", "password_value_printed": False}
    run_restic(configuration, ["init"], operation="initialize restic repository")
    run_restic(configuration, ["check"], operation="check initialized restic repository")
    return {"status": "initialized_and_checked", "password_value_printed": False}


def backup_manifest_directory(
    configuration: ResticConfiguration,
    backup_dir: Path,
    *,
    environment: str,
) -> dict[str, Any]:
    verification = clinical_backup.verify_backup(backup_dir, environment=environment)
    manifest = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    tier = str(manifest["tier"])
    # Snapshot with a project-relative path: restic reproduces the given path
    # layout on restore, and absolute Windows paths (drive letter + spaces +
    # depth) exceed MAX_PATH when the snapshot is restored for verification.
    relative_backup = backup_dir.resolve(strict=True).relative_to(PROJECT_ROOT)
    result = run_restic(
        configuration,
        [
            "backup",
            relative_backup.as_posix(),
            "--tag",
            "shen-clinical",
            "--tag",
            f"environment-{environment}",
            "--tag",
            f"tier-{tier}",
            "--json",
        ],
        operation="create encrypted restic snapshot",
    )
    summary = _last_json_record(result.stdout, "summary")
    run_restic(configuration, ["check"], operation="check restic repository")
    return {
        "status": "encrypted_snapshot_created_and_checked",
        "backup_id": verification["backup_id"],
        "snapshot_id": summary.get("snapshot_id"),
        "files_new": summary.get("files_new"),
        "files_changed": summary.get("files_changed"),
        "data_added": summary.get("data_added"),
        "manifest_verified_before_encryption": True,
        "password_value_printed": False,
    }


def check_repository(configuration: ResticConfiguration, *, read_data: bool) -> dict[str, Any]:
    arguments = ["check"]
    if read_data:
        arguments.append("--read-data")
    run_restic(configuration, arguments, operation="verify encrypted restic repository")
    return {
        "status": "repository_data_verified" if read_data else "repository_structure_verified",
        "read_data": read_data,
        "password_value_printed": False,
    }


def list_snapshots(configuration: ResticConfiguration) -> dict[str, Any]:
    result = run_restic(
        configuration,
        ["snapshots", "--tag", "shen-clinical", "--json"],
        operation="list restic snapshots",
    )
    snapshots = json.loads(result.stdout or "[]")
    return {
        "status": "snapshots_listed",
        "snapshot_count": len(snapshots),
        "snapshot_ids": [str(snapshot.get("short_id", "")) for snapshot in snapshots],
        "password_value_printed": False,
    }


def retention_plan(configuration: ResticConfiguration) -> dict[str, Any]:
    run_restic(
        configuration,
        [
            "forget",
            "--tag",
            "shen-clinical",
            "--keep-daily",
            "14",
            "--keep-weekly",
            "8",
            "--keep-monthly",
            "12",
            "--dry-run",
        ],
        operation="calculate restic retention plan",
    )
    return {
        "status": "retention_dry_run_completed",
        "policy": {"daily": 14, "weekly": 8, "monthly": 12},
        "snapshots_deleted": 0,
        "password_value_printed": False,
    }


def restore_verify(
    configuration: ResticConfiguration,
    snapshot: str,
    *,
    environment: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"(?:latest|[0-9a-fA-F]{4,64})", snapshot):
        raise ValueError("Snapshot must be 'latest' or a hexadecimal restic snapshot ID")
    RESTORE_TEST_ROOT.mkdir(parents=True, exist_ok=True)
    restore_root = (RESTORE_TEST_ROOT / f"verify-{uuid.uuid4().hex}").resolve()
    restore_root.mkdir(parents=False, exist_ok=False)
    try:
        run_restic(
            configuration,
            ["restore", snapshot, "--target", str(restore_root)],
            operation="restore encrypted restic snapshot",
        )
        manifest_paths = list(restore_root.rglob(MANIFEST_NAME))
        if len(manifest_paths) != 1:
            raise RuntimeError("Restored snapshot must contain exactly one clinical manifest")
        manifest_path = manifest_paths[0]
        if manifest_path.is_symlink():
            raise ValueError("Restored manifest symlinks are not allowed")
        restored_backup = manifest_path.parent
        verification = clinical_backup.verify_backup(
            restored_backup,
            environment=environment,
            allowed_root=restore_root,
        )
    finally:
        _remove_restore_tree(restore_root)
    return {
        "status": "encrypted_snapshot_restored_and_manifest_verified",
        "backup_id": verification["backup_id"],
        "file_count": verification["file_count"],
        "temporary_restore_removed": True,
        "password_value_printed": False,
    }


def _remove_restore_tree(path: Path) -> None:
    candidate = path.resolve(strict=True)
    try:
        relative = candidate.relative_to(RESTORE_TEST_ROOT)
    except ValueError as exc:
        raise RuntimeError("Refusing to remove a restore path outside the validation root") from exc
    if len(relative.parts) != 1 or not relative.name.startswith("verify-"):
        raise RuntimeError("Refusing to remove an unexpected restore path")
    shutil.rmtree(candidate, onexc=_make_writable_and_retry)


def _make_writable_and_retry(function, path: str, _error) -> None:
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    function(path)


def _last_json_record(output: str, message_type: str) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for line in output.splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("message_type") == message_type:
            records.append(record)
    if not records:
        raise RuntimeError("Restic did not return the expected machine-readable summary")
    return records[-1]


def main() -> int:
    args = build_parser().parse_args()
    try:
        configuration = load_configuration(args.environment, args.config_file)
        if args.command == "status":
            result = status(configuration)
        elif args.command == "init":
            result = initialize_repository(configuration)
        elif args.command == "backup":
            result = backup_manifest_directory(
                configuration,
                args.backup_dir,
                environment=args.environment,
            )
        elif args.command == "check":
            result = check_repository(configuration, read_data=args.read_data)
        elif args.command == "snapshots":
            result = list_snapshots(configuration)
        elif args.command == "retention-plan":
            result = retention_plan(configuration)
        else:
            result = restore_verify(
                configuration,
                args.snapshot,
                environment=args.environment,
            )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
