from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT_BACKUP_ROOT = (PROJECT_ROOT / "private_data" / "backups").resolve()
DEVELOPMENT_STORAGE_ROOT = (PROJECT_ROOT / "data" / "storage").resolve()
PRODUCTION_ENV_FILE = (PROJECT_ROOT / ".env.production.local").resolve()
MANIFEST_NAME = "backup_manifest.json"
IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")

CONFIG_FILES = [
    Path("compose.yaml"),
    Path("compose.production.yaml"),
    Path(".env.example"),
    Path("apps/api/alembic.ini"),
    Path("apps/api/alembic/env.py"),
    Path("apps/api/alembic/script.py.mako"),
    Path("apps/api/pyproject.toml"),
    Path("pnpm-lock.yaml"),
]


@dataclass(frozen=True)
class BackupProfile:
    name: str
    backup_root: Path
    storage_root: Path
    environment_file: Path
    compose_options: tuple[str, ...]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create, verify, or isolated-restore a private SHEN clinical backup."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--tier", choices=["daily", "weekly", "monthly"], required=True)
    create_parser.add_argument(
        "--environment", choices=["development", "production"], default="development"
    )

    for command in ("verify", "restore-test"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--backup-dir", type=Path, required=True)
        command_parser.add_argument(
            "--environment", choices=["development", "production"], default="development"
        )
    return parser


def backup_profile(environment: str) -> BackupProfile:
    if environment == "development":
        return BackupProfile(
            name="development",
            backup_root=DEVELOPMENT_BACKUP_ROOT,
            storage_root=DEVELOPMENT_STORAGE_ROOT,
            environment_file=PROJECT_ROOT / ".env",
            compose_options=("-f", str(PROJECT_ROOT / "compose.yaml")),
        )
    if environment != "production":
        raise ValueError("Unsupported backup environment")
    values = _read_environment(PRODUCTION_ENV_FILE)
    backup_root = _resolve_configured_path(values.get("PRODUCTION_BACKUP_ROOT"))
    storage_root = _resolve_configured_path(values.get("PRODUCTION_DOCUMENT_STORAGE"))
    if backup_root == DEVELOPMENT_BACKUP_ROOT or storage_root == DEVELOPMENT_STORAGE_ROOT:
        raise ValueError("Production backup/storage paths must be isolated from development")
    return BackupProfile(
        name="production",
        backup_root=backup_root,
        storage_root=storage_root,
        environment_file=PRODUCTION_ENV_FILE,
        compose_options=(
            "--project-name",
            "shen-production",
            "--env-file",
            str(PRODUCTION_ENV_FILE),
            "-f",
            str(PROJECT_ROOT / "compose.yaml"),
            "-f",
            str(PROJECT_ROOT / "compose.production.yaml"),
        ),
    )


def _read_environment(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"Required environment file is missing: {path.name}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        stripped = value.strip()
        if len(stripped) >= 2 and stripped[0] == stripped[-1] == "'":
            stripped = stripped[1:-1].replace("\\'", "'")
        elif len(stripped) >= 2 and stripped[0] == stripped[-1] == '"':
            stripped = stripped[1:-1]
        values[name.strip()] = stripped
    return values


def _resolve_configured_path(raw_path: str | None) -> Path:
    if not raw_path:
        raise ValueError("Production backup/storage path is missing")
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_backup_directory(path: Path, profile: BackupProfile, *, must_exist: bool) -> Path:
    candidate = path.resolve(strict=must_exist)
    try:
        relative = candidate.relative_to(profile.backup_root)
    except ValueError as exc:
        raise ValueError(f"Backup paths must remain under the {profile.name} backup root") from exc
    if not relative.parts:
        raise ValueError("A specific backup directory is required")
    current = profile.backup_root
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError("Backup symlinks are not allowed")
    return candidate


def run_compose(
    profile: BackupProfile,
    arguments: list[str],
    *,
    stdin_path: Path | None = None,
    stdout_path: Path | None = None,
    operation: str,
) -> subprocess.CompletedProcess[bytes]:
    command = ["docker", "compose", *profile.compose_options, *arguments]
    stdin_handle = stdin_path.open("rb") if stdin_path else None
    stdout_handle = stdout_path.open("wb") if stdout_path else subprocess.PIPE
    try:
        result = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            stdin=stdin_handle,
            stdout=stdout_handle,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        if stdin_handle:
            stdin_handle.close()
        if stdout_path and hasattr(stdout_handle, "close"):
            stdout_handle.close()
    if result.returncode != 0:
        raise RuntimeError(f"{operation} failed with exit code {result.returncode}")
    return result


def container_env(profile: BackupProfile, name: str) -> str:
    result = run_compose(
        profile,
        ["exec", "-T", "db", "printenv", name],
        operation=f"read container {name}",
    )
    value = (result.stdout or b"").decode("utf-8").strip()
    if not IDENTIFIER.fullmatch(value):
        raise RuntimeError(f"Container {name} is missing or unsafe")
    return value


def psql(profile: BackupProfile, database: str, user: str, query: str) -> str:
    if not IDENTIFIER.fullmatch(database) or not IDENTIFIER.fullmatch(user):
        raise ValueError("Unsafe database identifier")
    result = run_compose(
        profile,
        [
            "exec",
            "-T",
            "db",
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            user,
            "-d",
            database,
            "-At",
            "-c",
            query,
        ],
        operation="query backup metadata",
    )
    return (result.stdout or b"").decode("utf-8").strip()


def database_metadata(profile: BackupProfile, database: str, user: str) -> dict[str, Any]:
    table_names = [
        name
        for name in psql(
            profile,
            database,
            user,
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename",
        ).splitlines()
        if name
    ]
    if not table_names or any(not IDENTIFIER.fullmatch(name) for name in table_names):
        raise RuntimeError("Database table inventory is empty or unsafe")
    counts = {
        table: int(psql(profile, database, user, f'SELECT count(*) FROM "{table}"'))
        for table in table_names
    }
    revision = psql(profile, database, user, "SELECT version_num FROM alembic_version")
    return {
        "database": database,
        "user": user,
        "alembic_revision": revision,
        "table_counts": counts,
    }


def copy_regular_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError("Symlinks are not allowed in clinical backup sources")
        relative = path.relative_to(source)
        if (
            not path.is_file()
            or path.name == ".gitkeep"
            or "__pycache__" in relative.parts
            or path.suffix in {".pyc", ".pyo"}
        ):
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def manifest_entries(backup_dir: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in sorted(backup_dir.rglob("*")):
        if path.is_symlink():
            raise ValueError("Backup symlinks are not allowed")
        if not path.is_file() or path.name == MANIFEST_NAME:
            continue
        relative = path.relative_to(backup_dir).as_posix()
        entries.append(
            {
                "path": relative,
                "bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return entries


def create_backup(tier: str, environment: str = "development") -> dict[str, Any]:
    profile = backup_profile(environment)
    profile.backup_root.mkdir(parents=True, exist_ok=True)
    backup_id = f"{tier}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    backup_dir = safe_backup_directory(
        profile.backup_root / backup_id, profile, must_exist=False
    )
    backup_dir.mkdir(parents=False, exist_ok=False)

    database = container_env(profile, "POSTGRES_DB")
    user = container_env(profile, "POSTGRES_USER")
    run_compose(
        profile,
        ["exec", "-T", "db", "pg_isready", "-U", user, "-d", database],
        operation="check database readiness",
    )

    dump_path = backup_dir / "database" / "postgres.dump"
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    run_compose(
        profile,
        [
            "exec",
            "-T",
            "db",
            "pg_dump",
            "-U",
            user,
            "-d",
            database,
            "-Fc",
            "--no-owner",
            "--no-acl",
        ],
        stdout_path=dump_path,
        operation="create PostgreSQL backup",
    )

    copy_regular_tree(profile.storage_root, backup_dir / "documents")
    config_root = backup_dir / "configuration"
    for relative in CONFIG_FILES:
        source = PROJECT_ROOT / relative
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"Required backup configuration is missing: {relative.as_posix()}")
        target = config_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    copy_regular_tree(
        PROJECT_ROOT / "apps" / "api" / "alembic" / "versions",
        config_root / "apps" / "api" / "alembic" / "versions",
    )

    env_path = profile.environment_file
    manifest = {
        "schema_version": "1.1",
        "backup_id": backup_id,
        "created_at": datetime.now(UTC).isoformat(),
        "tier": tier,
        "environment": profile.name,
        "contains_clinical_data": True,
        "offsite_requires_client_side_encryption": True,
        "database": database_metadata(profile, database, user),
        "document_file_count": sum(
            1 for path in (backup_dir / "documents").rglob("*") if path.is_file()
        ),
        "environment_file_present": env_path.is_file(),
        "environment_file_sha256": sha256_file(env_path) if env_path.is_file() else None,
        "required_secret_recovery": [
            "database password",
            "admin password hash",
            "session/auth secret",
            "PII encryption master key",
            "PII HMAC secret",
            "enabled provider API keys",
        ],
        "files": manifest_entries(backup_dir),
    }
    manifest_path = backup_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    verification = verify_backup(backup_dir, environment=profile.name)
    return {
        "status": "created_and_verified",
        "backup_id": backup_id,
        "backup_dir": str(backup_dir.relative_to(PROJECT_ROOT)),
        "file_count": verification["file_count"],
        "tier": tier,
        "environment": profile.name,
    }


def verify_backup(
    raw_backup_dir: Path,
    environment: str = "development",
    *,
    allowed_root: Path | None = None,
) -> dict[str, Any]:
    profile = backup_profile(environment)
    if allowed_root is not None:
        profile = BackupProfile(
            name=profile.name,
            backup_root=allowed_root.resolve(strict=True),
            storage_root=profile.storage_root,
            environment_file=profile.environment_file,
            compose_options=profile.compose_options,
        )
    backup_dir = safe_backup_directory(raw_backup_dir, profile, must_exist=True)
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("Backup manifest is missing or unsafe")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") not in {"1.0", "1.1"} or not isinstance(
        manifest.get("files"), list
    ):
        raise ValueError("Unsupported backup manifest")
    manifest_environment = manifest.get("environment", "development")
    if manifest_environment != environment:
        raise ValueError("Backup manifest environment does not match the selected profile")

    expected: dict[str, dict[str, Any]] = {}
    for entry in manifest["files"]:
        relative = entry.get("path")
        if not isinstance(relative, str) or not relative or relative in expected:
            raise ValueError("Manifest paths must be non-empty and unique")
        candidate = (backup_dir / Path(relative)).resolve(strict=True)
        try:
            candidate.relative_to(backup_dir)
        except ValueError as exc:
            raise ValueError("Manifest path escapes the backup directory") from exc
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("Manifest entry is not a safe regular file")
        expected[relative] = entry

    actual = {
        path.relative_to(backup_dir).as_posix()
        for path in backup_dir.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }
    if actual != set(expected):
        raise ValueError("Backup file inventory differs from the manifest")

    for relative, entry in expected.items():
        path = backup_dir / Path(relative)
        if path.stat().st_size != int(entry["bytes"]):
            raise ValueError("Backup file size verification failed")
        if sha256_file(path) != entry["sha256"]:
            raise ValueError("Backup SHA-256 verification failed")
    return {
        "status": "verified",
        "backup_id": manifest.get("backup_id"),
        "file_count": len(expected),
        "alembic_revision": manifest["database"]["alembic_revision"],
    }


def restore_test(raw_backup_dir: Path, environment: str = "development") -> dict[str, Any]:
    profile = backup_profile(environment)
    backup_dir = safe_backup_directory(raw_backup_dir, profile, must_exist=True)
    verification = verify_backup(backup_dir, environment=environment)
    manifest = json.loads((backup_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    user = str(manifest["database"]["user"])
    if not IDENTIFIER.fullmatch(user):
        raise ValueError("Unsafe database user in manifest")
    restore_database = f"shen_restore_verify_{uuid.uuid4().hex[:12]}"
    if not IDENTIFIER.fullmatch(restore_database):
        raise RuntimeError("Generated restore database name is unsafe")
    dump_path = backup_dir / "database" / "postgres.dump"
    created = False
    try:
        run_compose(
            profile,
            ["exec", "-T", "db", "createdb", "-U", user, restore_database],
            operation="create isolated restore database",
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
                restore_database,
                "--no-owner",
                "--no-acl",
                "--exit-on-error",
            ],
            stdin_path=dump_path,
            operation="restore isolated database",
        )
        restored = database_metadata(profile, restore_database, user)
        expected = manifest["database"]
        if restored["alembic_revision"] != expected["alembic_revision"]:
            raise RuntimeError("Restored Alembic revision does not match backup")
        if restored["table_counts"] != expected["table_counts"]:
            raise RuntimeError("Restored database table counts do not match backup")
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
                    restore_database,
                ],
                operation="remove isolated restore database",
            )
    return {
        "status": "isolated_restore_verified",
        "backup_id": verification["backup_id"],
        "alembic_revision": verification["alembic_revision"],
        "table_count": len(manifest["database"]["table_counts"]),
        "isolated_database_removed": True,
    }


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "create":
        result = create_backup(args.tier, environment=args.environment)
    elif args.command == "verify":
        result = verify_backup(args.backup_dir, environment=args.environment)
    else:
        result = restore_test(args.backup_dir, environment=args.environment)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
