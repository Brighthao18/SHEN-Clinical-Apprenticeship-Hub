"""Health endpoint contract: /health is readiness, /health/live is liveness."""

from __future__ import annotations

from fastapi.testclient import TestClient


class _BrokenEngine:
    def connect(self):
        raise RuntimeError("database unreachable")


def test_health_reports_ready_when_database_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["database"] == "ok"
    assert body["service"] == "shen-clinical-api"


def test_health_fails_closed_when_database_unavailable(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("app.main.engine", _BrokenEngine())
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["database"] == "unavailable"


def test_health_live_stays_alive_without_database(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("app.main.engine", _BrokenEngine())
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json()["status"] == "alive"
