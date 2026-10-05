from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_ROOT))

from bootstrap_production import (  # noqa: E402
    initialize_environment,
    normalize_environment_file,
    parse_env_file,
    validate_environment,
)


def test_bootstrap_writes_only_hashes_and_distinct_high_entropy_secrets(tmp_path) -> None:
    env_path = tmp_path / ".env.production.local"
    private_root = tmp_path / "private_production"
    plaintext = "Local-Only-Clinical-Keyphrase-2026!"

    result = initialize_environment(
        env_path,
        password_reader=lambda: plaintext,
        production_root=private_root,
    )
    values = parse_env_file(env_path)

    assert result["secret_values_printed"] is False
    assert values["APP_ADMIN_PASSWORD"] == ""
    assert plaintext not in env_path.read_text(encoding="utf-8")
    assert values["APP_ADMIN_PASSWORD_HASH"].startswith("pbkdf2_sha256$600000$")
    assert values["PII_ENCRYPTION_SECRET"] != values["PII_HASH_SECRET"]
    assert len(values["JWT_SECRET"]) >= 80
    assert Path(values["RESTIC_PASSWORD_FILE"]).is_file()
    assert "APP_ADMIN_PASSWORD_HASH='pbkdf2_sha256$" in env_path.read_text(encoding="utf-8")

    validation = validate_environment(env_path, validate_compose=False)
    assert validation["status"] == "production_readiness_configuration_valid"
    assert validation["real_data_import_authorized"] is False


def test_bootstrap_refuses_to_overwrite_existing_environment(tmp_path) -> None:
    env_path = tmp_path / ".env.production.local"
    env_path.write_text("existing=true\n", encoding="utf-8")

    with pytest.raises(FileExistsError, match="不会覆盖"):
        initialize_environment(
            env_path,
            password_reader=lambda: "Local-Only-Clinical-Keyphrase-2026!",
            production_root=tmp_path / "private_production",
        )


def test_existing_unquoted_dollar_values_are_normalized_without_value_disclosure(
    tmp_path, monkeypatch
) -> None:
    env_path = tmp_path / ".env.production.local"
    private_root = tmp_path / "private_production"
    initialize_environment(
        env_path,
        password_reader=lambda: "Local-Only-Clinical-Keyphrase-2026!",
        production_root=private_root,
    )
    original_values = parse_env_file(env_path)
    env_path.write_text(
        env_path.read_text(encoding="utf-8").replace(
            "APP_ADMIN_PASSWORD_HASH='" + original_values["APP_ADMIN_PASSWORD_HASH"] + "'",
            "APP_ADMIN_PASSWORD_HASH=" + original_values["APP_ADMIN_PASSWORD_HASH"],
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("bootstrap_production.PRODUCTION_ROOT", private_root)

    with pytest.raises(ValueError, match="unquoted"):
        validate_environment(env_path, validate_compose=False)

    result = normalize_environment_file(env_path)

    assert result["secret_values_printed"] is False
    assert parse_env_file(env_path) == original_values
    assert list((private_root / "secrets" / "env_history").glob("*.txt"))
