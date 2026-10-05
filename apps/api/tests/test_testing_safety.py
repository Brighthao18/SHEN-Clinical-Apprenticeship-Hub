"""Tests for the suite's database-isolation guard (tests/testing_safety.py)."""

from __future__ import annotations

import pytest
from testing_safety import TestDatabaseRejected, resolve_test_database_url

BUSINESS_PG = "postgresql+psycopg://shen:secret@localhost:5432/shen"


def test_defaults_to_memory_sqlite_without_test_url() -> None:
    assert resolve_test_database_url({}) == "sqlite+pysqlite:///:memory:"


def test_explicit_memory_sqlite_is_always_safe() -> None:
    env = {
        "DATABASE_URL": BUSINESS_PG,
        "TEST_DATABASE_URL": "sqlite+pysqlite:///:memory:",
    }
    assert resolve_test_database_url(env) == "sqlite+pysqlite:///:memory:"


def test_accepts_dedicated_postgres_test_database() -> None:
    env = {
        "DATABASE_URL": BUSINESS_PG,
        "TEST_DATABASE_URL": "postgresql+psycopg://shen:secret@localhost:55499/shen_test",
    }
    assert resolve_test_database_url(env) == (
        "postgresql+psycopg://shen:secret@localhost:55499/shen_test"
    )


def test_rejects_test_url_equal_to_business_database_url() -> None:
    env = {"DATABASE_URL": BUSINESS_PG, "TEST_DATABASE_URL": BUSINESS_PG}
    with pytest.raises(TestDatabaseRejected):
        resolve_test_database_url(env)


def test_rejects_business_database_name_without_test_marker() -> None:
    env = {
        "DATABASE_URL": BUSINESS_PG,
        "TEST_DATABASE_URL": "postgresql+psycopg://shen:secret@localhost:55499/shen",
    }
    with pytest.raises(TestDatabaseRejected):
        resolve_test_database_url(env)


def test_rejects_sqlite_file_without_test_marker() -> None:
    env = {
        "DATABASE_URL": "sqlite+pysqlite:///./work/shen-dev.db",
        "TEST_DATABASE_URL": "sqlite+pysqlite:///./work/shen-dev.db.copy",
    }
    with pytest.raises(TestDatabaseRejected):
        resolve_test_database_url(env)


def test_accepts_sqlite_file_with_test_marker() -> None:
    env = {"TEST_DATABASE_URL": "sqlite+pysqlite:///./work/shen-test.db"}
    assert resolve_test_database_url(env) == "sqlite+pysqlite:///./work/shen-test.db"


def test_rejects_malformed_url_instead_of_guessing() -> None:
    with pytest.raises(TestDatabaseRejected):
        resolve_test_database_url({"TEST_DATABASE_URL": "not-a-database-url"})


@pytest.mark.parametrize("candidate", [
    "postgresql://other:changed@127.0.0.1/shen_test?sslmode=require",
    "postgresql+psycopg://shen:secret@localhost:5432/shen_test",
])
def test_rejects_same_target_despite_credentials_driver_or_options(candidate: str) -> None:
    with pytest.raises(TestDatabaseRejected):
        resolve_test_database_url({
            "DATABASE_URL": "postgresql+psycopg://shen:secret@localhost/shen_test",
            "TEST_DATABASE_URL": candidate,
        })


@pytest.mark.parametrize("candidate", [
    "sqlite:///./test-data/clinical.db",
    "postgresql://localhost/latest",
    "postgresql://localhost/shen_test?host=production",
    "postgresql://localhost/shen_test?service=production",
    "sqlite:///./work/shen_test.db?uri=true",
])
def test_rejects_misleading_markers_and_target_overrides(candidate: str) -> None:
    with pytest.raises(TestDatabaseRejected):
        resolve_test_database_url({"TEST_DATABASE_URL": candidate})


def test_invalid_url_does_not_expose_secret() -> None:
    with pytest.raises(TestDatabaseRejected) as rejected:
        resolve_test_database_url({"TEST_DATABASE_URL": "broken-user:private-secret@host"})
    assert "private-secret" not in str(rejected.value)
