from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import string
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.production.local"
PRODUCTION_ROOT = PROJECT_ROOT / "private_data" / "production"
COMPOSE_FILES = (PROJECT_ROOT / "compose.yaml", PROJECT_ROOT / "compose.production.yaml")

REQUIRED_VALUES = {
    "APP_ENV",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "DATABASE_URL",
    "APP_ADMIN_USERNAME",
    "APP_ADMIN_PASSWORD_HASH",
    "JWT_SECRET",
    "PII_ENCRYPTION_SECRET",
    "PII_HASH_SECRET",
    "PRODUCTION_DOCUMENT_STORAGE",
    "PRODUCTION_BACKUP_ROOT",
    "RESTIC_REPOSITORY",
    "RESTIC_PASSWORD_FILE",
}
SECRET_NAMES = {
    "POSTGRES_PASSWORD",
    "APP_ADMIN_PASSWORD",
    "APP_ADMIN_PASSWORD_HASH",
    "JWT_SECRET",
    "PII_ENCRYPTION_SECRET",
    "PII_HASH_SECRET",
    "MINERU_API_TOKEN",
}
SAFE_DATABASE_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bootstrap or validate the isolated SHEN production environment."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser(
        "init", help="Interactively create production secrets on this machine."
    )
    init_parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    validate_parser = subparsers.add_parser(
        "validate", help="Fail closed unless production configuration is ready."
    )
    validate_parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    validate_parser.add_argument(
        "--skip-compose",
        action="store_true",
        help="Validate values only; intended for isolated unit tests.",
    )
    normalize_parser = subparsers.add_parser(
        "normalize-env",
        help="Atomically quote an existing production env file for safe Compose parsing.",
    )
    normalize_parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    return parser


def hash_admin_password(password: str, *, iterations: int = 600_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def validate_admin_password_strength(password: str) -> None:
    if len(password) < 16:
        raise ValueError("管理员密码至少需要 16 个字符")
    categories = sum(
        (
            any(character.islower() for character in password),
            any(character.isupper() for character in password),
            any(character.isdigit() for character in password),
            any(not character.isalnum() for character in password),
        )
    )
    if len(password) < 24 and categories < 3:
        raise ValueError("管理员密码需包含至少三类字符，或使用不少于 24 字符的长口令")
    lowered = password.casefold()
    if any(fragment in lowered for fragment in ("password", "shen-dev", "123456", "adminadmin")):
        raise ValueError("管理员密码包含常见或开发环境口令片段")


def read_admin_password() -> str:
    if not sys.stdin.isatty():
        raise RuntimeError("生产 bootstrap 必须在本机交互式终端运行")
    password = getpass.getpass("请输入新的生产管理员密码（输入不会显示）: ")
    confirmation = getpass.getpass("请再次输入生产管理员密码: ")
    if not secrets.compare_digest(password, confirmation):
        raise ValueError("两次输入的管理员密码不一致")
    validate_admin_password_strength(password)
    return password


def _random_database_password(length: int = 48) -> str:
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _write_private_file_exclusive(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as destination:
            destination.write(content)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows ACLs are host-specific. The file remains ignored and under private_data.
        pass


def _env_lines(values: dict[str, str]) -> str:
    order = [
        "COMPOSE_PROJECT_NAME",
        "APP_ENV",
        "APP_VERSION",
        "APP_TIMEZONE",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "DATABASE_URL",
        "REDIS_URL",
        "TASKS_EAGER",
        "CELERY_RESULT_EXPIRES_SECONDS",
        "PRODUCTION_DOCUMENT_STORAGE",
        "PRODUCTION_BACKUP_ROOT",
        "MAX_UPLOAD_BYTES",
        "APP_ADMIN_USERNAME",
        "APP_ADMIN_PASSWORD",
        "APP_ADMIN_PASSWORD_HASH",
        "JWT_SECRET",
        "JWT_EXPIRE_MINUTES",
        "PII_ENCRYPTION_SECRET",
        "PII_HASH_SECRET",
        "OCR_PROVIDER",
        "MINERU_BASE_URL",
        "MINERU_API_TOKEN",
        "MINERU_MODEL_VERSION",
        "MINERU_LANGUAGE",
        "MINERU_POLL_INTERVAL_SECONDS",
        "MINERU_TIMEOUT_SECONDS",
        "ALLOW_IDENTIFIABLE_MINERU_UPLOAD",
        "WEB_ORIGIN",
        "PRODUCTION_WEB_PORT",
        "PRODUCTION_API_PORT",
        "NEXT_PUBLIC_APP_NAME",
        "COOKIE_SECURE",
        "RESTIC_REPOSITORY",
        "RESTIC_PASSWORD_FILE",
        "RESTIC_EXECUTABLE",
    ]
    header = (
        "# Generated locally by scripts/bootstrap_production.py.\n"
        "# Contains secrets. Never commit, share, paste into chat, or place in ordinary backups.\n"
    )
    return header + "".join(f"{name}={_quote_env_value(values[name])}\n" for name in order)


def _quote_env_value(value: str) -> str:
    """Use Compose literal quoting so random '$' bytes are never interpolated."""

    return "'" + value.replace("'", "\\'") + "'"


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("\\'", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def initialize_environment(
    env_file: Path,
    *,
    password_reader: Callable[[], str] = read_admin_password,
    production_root: Path = PRODUCTION_ROOT,
) -> dict[str, object]:
    env_path = env_file.expanduser().resolve()
    if env_path.exists():
        raise FileExistsError("生产环境文件已存在；bootstrap 不会覆盖或轮换现有密钥")

    password = password_reader()
    validate_admin_password_strength(password)
    admin_hash = hash_admin_password(password)
    password = ""  # Drop the plaintext reference before any filesystem writes.

    postgres_password = _random_database_password()
    database_url = (
        "postgresql+psycopg://shen_prod:"
        f"{quote(postgres_password, safe='-_')}@db:5432/shen_production"
    )
    private_root = production_root.expanduser().resolve()
    restic_password_path = private_root / "secrets" / "restic_password.txt"
    document_storage = private_root / "document_storage"
    backup_root = private_root / "backups"
    restic_repository = private_root / "restic_repository"
    for directory in (document_storage, backup_root, restic_repository, restic_password_path.parent):
        directory.mkdir(parents=True, exist_ok=True)

    values = {
        "COMPOSE_PROJECT_NAME": "shen-production",
        "APP_ENV": "production",
        "APP_VERSION": "0.2.0-rc.1",
        "APP_TIMEZONE": "Asia/Singapore",
        "POSTGRES_DB": "shen_production",
        "POSTGRES_USER": "shen_prod",
        "POSTGRES_PASSWORD": postgres_password,
        "DATABASE_URL": database_url,
        "REDIS_URL": "redis://redis:6379/0",
        "TASKS_EAGER": "false",
        "CELERY_RESULT_EXPIRES_SECONDS": "604800",
        "PRODUCTION_DOCUMENT_STORAGE": _configuration_path(document_storage),
        "PRODUCTION_BACKUP_ROOT": _configuration_path(backup_root),
        "MAX_UPLOAD_BYTES": "26214400",
        "APP_ADMIN_USERNAME": "admin",
        "APP_ADMIN_PASSWORD": "",
        "APP_ADMIN_PASSWORD_HASH": admin_hash,
        "JWT_SECRET": secrets.token_urlsafe(64),
        "JWT_EXPIRE_MINUTES": "480",
        "PII_ENCRYPTION_SECRET": secrets.token_urlsafe(64),
        "PII_HASH_SECRET": secrets.token_urlsafe(64),
        "OCR_PROVIDER": "mock",
        "MINERU_BASE_URL": "https://mineru.net",
        "MINERU_API_TOKEN": "",
        "MINERU_MODEL_VERSION": "vlm",
        "MINERU_LANGUAGE": "ch",
        "MINERU_POLL_INTERVAL_SECONDS": "3",
        "MINERU_TIMEOUT_SECONDS": "300",
        "ALLOW_IDENTIFIABLE_MINERU_UPLOAD": "false",
        "WEB_ORIGIN": "http://localhost:3100",
        "PRODUCTION_WEB_PORT": "3100",
        "PRODUCTION_API_PORT": "8100",
        "NEXT_PUBLIC_APP_NAME": "师承临床知识库",
        "COOKIE_SECURE": "false",
        "RESTIC_REPOSITORY": _configuration_path(restic_repository),
        "RESTIC_PASSWORD_FILE": _configuration_path(restic_password_path),
        "RESTIC_EXECUTABLE": "",
    }

    restic_created = False
    try:
        _write_private_file_exclusive(
            restic_password_path,
            secrets.token_urlsafe(64) + "\n",
        )
        restic_created = True
        _write_private_file_exclusive(env_path, _env_lines(values))
    except Exception:
        if restic_created:
            restic_password_path.unlink(missing_ok=True)
        raise

    return {
        "status": "production_secrets_bootstrapped",
        "environment_file": _display_path(env_path),
        "plaintext_admin_password_persisted": False,
        "secret_values_printed": False,
        "next_required_action": (
            "从本机环境文件中把 PII encryption master key 和 restic 密码"
            "分别保存到密码管理器；不要把值发送给 Codex。"
        ),
    }


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"环境文件第 {line_number} 行缺少等号")
        name, value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or name in values:
            raise ValueError(f"环境文件第 {line_number} 行变量名无效或重复")
        values[name] = _unquote_env_value(value.strip())
    return values


def normalize_environment_file(env_file: Path) -> dict[str, object]:
    """Rewrite only dotenv quoting while retaining a protected recovery copy."""

    env_path = env_file.expanduser().resolve(strict=True)
    values = parse_env_file(env_path)
    missing = sorted(name for name in REQUIRED_VALUES if name not in values)
    if missing:
        raise ValueError("Production environment is missing required values")
    history = PRODUCTION_ROOT / "secrets" / "env_history"
    history.mkdir(parents=True, exist_ok=True)
    backup = history / f"env-before-compose-quoting-{secrets.token_hex(8)}.txt"
    _write_private_file_exclusive(backup, env_path.read_text(encoding="utf-8"))
    temporary = env_path.with_name(f".{env_path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_text(_env_lines(values), encoding="utf-8", newline="\n")
        os.replace(temporary, env_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "status": "production_environment_compose_quoting_normalized",
        "environment_file": _display_path(env_path),
        "protected_recovery_copy_created": True,
        "secret_values_printed": False,
    }


def _contains_unsafe_compose_interpolation(path: Path) -> bool:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        _, raw_value = line.split("=", 1)
        value = raw_value.strip()
        if "$" in value and not (len(value) >= 2 and value[0] == value[-1] == "'"):
            return True
    return False


def _resolve_local_path(raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


def _ensure_distinct_production_paths(values: dict[str, str]) -> None:
    document_storage = _resolve_local_path(values["PRODUCTION_DOCUMENT_STORAGE"])
    backup_root = _resolve_local_path(values["PRODUCTION_BACKUP_ROOT"])
    password_file = _resolve_local_path(values["RESTIC_PASSWORD_FILE"])
    development_storage = (PROJECT_ROOT / "data" / "storage").resolve()
    development_backup = (PROJECT_ROOT / "private_data" / "backups").resolve()
    if document_storage == development_storage:
        raise ValueError("Production Document storage cannot use the development storage root")
    if backup_root == development_backup:
        raise ValueError("Production backup root cannot use the development backup root")
    if document_storage == backup_root or _is_within(backup_root, document_storage):
        raise ValueError("Production backup and Document storage paths must be separate")
    if not password_file.is_file():
        raise ValueError("RESTIC_PASSWORD_FILE is missing")
    repository = values["RESTIC_REPOSITORY"]
    if "://" not in repository and not repository.startswith(("s3:", "sftp:", "rest:")):
        repository_path = _resolve_local_path(repository.removeprefix("local:"))
        if repository_path in {document_storage, backup_root}:
            raise ValueError("Restic repository must be separate from backup and Document roots")
        if _is_within(repository_path, backup_root) or _is_within(repository_path, document_storage):
            raise ValueError("Restic repository cannot be nested inside clinical source roots")


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return True
    except ValueError:
        return False


def _validate_settings(values: dict[str, str]) -> None:
    sys.path.insert(0, str(PROJECT_ROOT / "apps" / "api"))
    from app.core.config import Settings

    setting_names = {
        "APP_ENV",
        "APP_VERSION",
        "APP_TIMEZONE",
        "DATABASE_URL",
        "REDIS_URL",
        "TASKS_EAGER",
        "CELERY_RESULT_EXPIRES_SECONDS",
        "MAX_UPLOAD_BYTES",
        "APP_ADMIN_USERNAME",
        "APP_ADMIN_PASSWORD",
        "APP_ADMIN_PASSWORD_HASH",
        "JWT_SECRET",
        "JWT_EXPIRE_MINUTES",
        "PII_ENCRYPTION_SECRET",
        "PII_HASH_SECRET",
        "OCR_PROVIDER",
        "MINERU_BASE_URL",
        "MINERU_API_TOKEN",
        "MINERU_MODEL_VERSION",
        "MINERU_LANGUAGE",
        "MINERU_POLL_INTERVAL_SECONDS",
        "MINERU_TIMEOUT_SECONDS",
        "ALLOW_IDENTIFIABLE_MINERU_UPLOAD",
        "WEB_ORIGIN",
    }
    kwargs = {name.lower(): values[name] for name in setting_names if name in values}
    Settings(_env_file=None, **kwargs)


def _validate_compose(env_file: Path) -> None:
    command = [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(COMPOSE_FILES[0]),
        "-f",
        str(COMPOSE_FILES[1]),
        "config",
        "--format",
        "json",
    ]
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Production Compose configuration validation failed")
    payload = json.loads(result.stdout)
    if payload.get("name") != "shen-production":
        raise ValueError("Production Compose project name is not isolated")
    services = payload.get("services", {})
    for service_name in ("db", "redis"):
        if services.get(service_name, {}).get("ports"):
            raise ValueError(f"Production {service_name} must not publish a host port")
    for service_name in ("api", "web"):
        ports = services.get(service_name, {}).get("ports", [])
        if not ports or any(port.get("host_ip") != "127.0.0.1" for port in ports):
            raise ValueError(f"Production {service_name} ports must bind only to 127.0.0.1")
    db_sources = {volume.get("source") for volume in services["db"].get("volumes", [])}
    redis_sources = {volume.get("source") for volume in services["redis"].get("volumes", [])}
    if "shen_production_postgres_data" not in db_sources:
        raise ValueError("Production PostgreSQL volume is not isolated")
    if "shen_production_redis_data" not in redis_sources:
        raise ValueError("Production Redis volume is not isolated")
    for service_name in ("api", "worker"):
        environment = services[service_name].get("environment", {})
        if environment.get("APP_ENV") != "production" or environment.get("APP_ADMIN_PASSWORD"):
            raise ValueError(f"Production {service_name} environment is unsafe")
    browser_environment = services["web"].get("environment", {})
    if any(name in browser_environment for name in SECRET_NAMES):
        raise ValueError("Provider or application secrets leaked into the Web service")


def validate_environment(env_file: Path, *, validate_compose: bool = True) -> dict[str, object]:
    env_path = env_file.expanduser().resolve(strict=True)
    if _contains_unsafe_compose_interpolation(env_path):
        raise ValueError(
            "Production environment contains an unquoted '$'; run normalize-env before Compose"
        )
    values = parse_env_file(env_path)
    missing = sorted(name for name in REQUIRED_VALUES if not values.get(name))
    if missing:
        raise ValueError("Production environment is missing required values")
    if values.get("APP_ENV", "").casefold() != "production":
        raise ValueError("APP_ENV must be production")
    if values.get("APP_ADMIN_PASSWORD"):
        raise ValueError("Plaintext APP_ADMIN_PASSWORD must be empty in production")
    for identifier_name in ("POSTGRES_DB", "POSTGRES_USER"):
        if not SAFE_DATABASE_IDENTIFIER.fullmatch(values[identifier_name]):
            raise ValueError(f"{identifier_name} is not a safe PostgreSQL identifier")
    _validate_settings(values)
    _ensure_distinct_production_paths(values)
    if validate_compose:
        _validate_compose(env_path)
    return {
        "status": "production_readiness_configuration_valid",
        "environment_file": _display_path(env_path),
        "compose_isolation_validated": validate_compose,
        "plaintext_admin_password_persisted": False,
        "secret_values_printed": False,
        "real_data_import_authorized": False,
        "remaining_gate": (
            "Restic init/backup/restore, MinerU case-scoped calibration, and human Gold confirmation"
        ),
    }


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _configuration_path(path: Path) -> str:
    try:
        return "./" + path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "init":
            result = initialize_environment(args.env_file)
        elif args.command == "normalize-env":
            result = normalize_environment_file(args.env_file)
        else:
            result = validate_environment(
                args.env_file,
                validate_compose=not args.skip_compose,
            )
    except (FileExistsError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
