"""Explicit test-database isolation for the backend suite.

Every test drops and recreates all model tables (``drop_all``), so the suite
must never run against a database the application itself uses. This module
decides the suite's database URL and rejects business targets BEFORE any
connection is opened or any table is dropped.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

DEFAULT_TEST_DATABASE_URL = "sqlite+pysqlite:///:memory:"


class TestDatabaseRejected(RuntimeError):
    """A candidate test database looks like a business database."""


def _safe_url(candidate: str):
    try:
        return make_url(candidate)
    except (ArgumentError, ValueError):  # never echo credentials from a malformed URL
        raise TestDatabaseRejected(
            "TEST_DATABASE_URL is not a valid database URL"
        ) from None


def _target(url):
    backend = url.get_backend_name()
    if backend == "sqlite":
        return (backend, str(Path(url.database or "").resolve()).casefold())
    host = (url.host or "localhost").lower()
    if host in {"127.0.0.1", "::1"}:
        host = "localhost"
    return (backend, host, url.port or 5432, url.database)


def resolve_test_database_url(env: Mapping[str, str]) -> str:
    """Pick the suite database URL; refuse business databases up front.

    Sources, in order:

    1. ``TEST_DATABASE_URL`` — an explicitly test-designated address;
    2. the in-memory SQLite default.

    The application's ``DATABASE_URL`` is used only to reject the same
    target, never as the test address. Credentials, drivers and ordinary
    connection options do not establish a separate database. A persistent
    target must have a separate "test" token in its database basename;
    parent-directory markers are insufficient. SQLite in-memory databases
    are always accepted because they cannot persist anything.
    """

    candidate = (env.get("TEST_DATABASE_URL") or "").strip()
    if not candidate:
        return DEFAULT_TEST_DATABASE_URL

    url = _safe_url(candidate)
    if url.get_backend_name() == "sqlite" and not url.query and (
        url.database or ""
    ) in {":memory:", ""}:
        return candidate
    if url.get_backend_name() not in {"sqlite", "postgresql"}:
        raise TestDatabaseRejected("Only SQLite and PostgreSQL test targets are supported")
    # Connection query parameters can override the target validated below.
    if any(key.lower() in {"host", "hostaddr", "port", "dbname", "database", "service", "uri"}
           for key in url.query):
        raise TestDatabaseRejected("Test URL must not override its target through query parameters")
    business = (env.get("DATABASE_URL") or "").strip()
    if business and _target(url) == _target(_safe_url(business)):
        raise TestDatabaseRejected(
            "TEST_DATABASE_URL equals the application DATABASE_URL. Tests drop and "
            "recreate every table per test; they must never run against the "
            "business database. Point TEST_DATABASE_URL at a dedicated test "
            "database instead."
        )

    name = Path(url.database or "").name if url.get_backend_name() == "sqlite" else url.database
    if not re.search(r"(^|[_.-])test([_.-]|$)", name or "", re.IGNORECASE):
        raise TestDatabaseRejected(
            "Test database basename must contain a separate test marker (e.g. shen_test)"
        )
    return candidate
