from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from testing_safety import TestDatabaseRejected, resolve_test_database_url

# Every test drops and recreates all model tables, so the suite database is
# decided through an explicit, validated test address BEFORE the application
# imports (and its engine) exist. The application's own DATABASE_URL is never
# inherited: resolve_test_database_url rejects business targets up front.
# Point TEST_DATABASE_URL at a dedicated database (separate "test" name token,
# e.g. postgresql+psycopg://.../shen_test) to run against a real engine.
try:
    _TEST_DATABASE_URL = resolve_test_database_url(os.environ)
except TestDatabaseRejected as exc:
    raise pytest.UsageError(f"Refusing to start the test suite: {exc}") from exc
os.environ["DATABASE_URL"] = _TEST_DATABASE_URL

os.environ["TASKS_EAGER"] = "true"
os.environ["OCR_PROVIDER"] = "mock"
os.environ["AUTH_DISABLED"] = "false"
os.environ["APP_ADMIN_USERNAME"] = "admin"
os.environ["APP_ADMIN_PASSWORD"] = "test-password"
os.environ["JWT_SECRET"] = "test-jwt-secret-that-is-long-and-isolated"
os.environ["PII_ENCRYPTION_SECRET"] = "test-encryption-secret"
os.environ["PII_HASH_SECRET"] = "test-hmac-secret"
os.environ["STORAGE_ROOT"] = "../../work/test-storage"

from app.db.base import Base  # noqa: E402
from app.db.session import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def reset_database() -> Generator[None, None, None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    with SessionLocal() as session:
        session.rollback()


@pytest.fixture
def db_session():
    with SessionLocal() as session:
        yield session


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/token",
        data={"username": "admin", "password": "test-password"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
