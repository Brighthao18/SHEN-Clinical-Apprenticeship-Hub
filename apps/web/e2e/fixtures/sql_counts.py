"""Per-request SQL count/time for the heavy list endpoints (P1-B measurement).

Run from apps/api with the venv python while shen_e2e holds the target scale:
  DATABASE_URL=... python - < ../web/e2e/fixtures/sql_counts.py <patient_id> <note_id>

Uses the in-process TestClient (full ASGI stack, no network) with a SQLAlchemy
event listener; per endpoint reports query count and total SQL seconds. These
counts explain WHERE time goes; the latency baseline itself is measured over
HTTP by e2e/perf/measure.mjs.
"""

from __future__ import annotations

import json
import sys
import time

DATABASE_URL = "postgresql+psycopg://shen:shen-e2e-password@127.0.0.1:5433/shen_e2e"

import os

os.environ["DATABASE_URL"] = DATABASE_URL

from sqlalchemy import event

from app.db.session import engine
from app.main import app

stats = {"count": 0, "seconds": 0.0}


@event.listens_for(engine, "before_cursor_execute")
def _before(conn, cursor, statement, parameters, context, executemany):
    context._perf_started = time.perf_counter()


@event.listens_for(engine, "after_cursor_execute")
def _after(conn, cursor, statement, parameters, context, executemany):
    stats["count"] += 1
    stats["seconds"] += time.perf_counter() - getattr(context, "_perf_started", time.perf_counter())


from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


def main() -> int:
    patient_id = sys.argv[1]
    note_id = sys.argv[2]
    token_response = client.post(
        "/api/v1/auth/token", data={"username": "admin", "password": "shen-dev-only"}
    )
    token = token_response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    endpoints = [
        ("library_list_limit20", "/api/v1/notes?limit=20"),
        ("dashboard_in_progress_query", "/api/v1/notes?finalized=false&limit=3"),
        ("dashboard_aggregate", "/api/v1/dashboard"),
        ("patient_notes_python_assembled", f"/api/v1/patients/{patient_id}/notes"),
        ("note_history_detail", f"/api/v1/notes/{note_id}"),
    ]
    results = []
    for name, path in endpoints:
        stats["count"] = 0
        stats["seconds"] = 0.0
        started = time.perf_counter()
        response = client.get(path, headers=headers)
        wall = time.perf_counter() - started
        results.append(
            {
                "endpoint": name,
                "status": response.status_code,
                "wall_ms": round(wall * 1000, 1),
                "sql_count": stats["count"],
                "sql_seconds": round(stats["seconds"] * 1000, 1),
            }
        )
        print(json.dumps(results[-1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
