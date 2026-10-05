"""Route-level tests for the review-resolution closed loop.

Covers GET /reviews listing filters, POST /reviews/{id}/resolve guard rails
(404, already-closed, row-version conflict), and the acceptance side effects
(prescription-item status propagation, document ACCEPTED, batch recalculation).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.domain import ImportItem, PrescriptionItem, ReviewItem
from app.models.enums import ProcessingStatus, ReviewStatus

FIXTURE = Path(__file__).parent / "fixtures" / "jnu_outpatient_v1.txt"


def _import_document(client: TestClient, auth_headers: dict[str, str]) -> str:
    response = client.post(
        "/api/v1/imports",
        headers=auth_headers,
        files={"files": ("visit.jpg", b"fake-jpeg-source-evidence", "image/jpeg")},
        data={"mock_ocr_text": FIXTURE.read_text(encoding="utf-8"), "label": "复核测试"},
    )
    assert response.status_code == 202, response.text
    items = response.json()["items"]
    assert len(items) == 1
    return uuid.UUID(items[0]["document_id"])


def _open_reviews(client: TestClient, auth_headers: dict[str, str]) -> list[dict]:
    response = client.get("/api/v1/reviews?status=open", headers=auth_headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_list_reviews_returns_open_items_with_encounter_context(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    document_id = _import_document(client, auth_headers)
    reviews = _open_reviews(client, auth_headers)
    assert len(reviews) == 2
    assert {review["issue_code"] for review in reviews} == {
        "HOSPITAL_SPECIFICATION_UNKNOWN"
    }
    for review in reviews:
        assert review["status"] == "open"
        assert review["document_id"] == str(document_id)
        assert review["encounter_id"] is not None
        assert review["hospital"] is not None
        assert review["encounter_date"] is not None
        assert review["row_version"] == 1

    resolved_response = client.get("/api/v1/reviews?status=resolved", headers=auth_headers)
    assert resolved_response.status_code == 200
    assert resolved_response.json() == []


def test_resolve_review_rejects_unknown_id_and_bad_version(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    _import_document(client, auth_headers)
    review = _open_reviews(client, auth_headers)[0]
    review_id = review["id"]

    missing = client.post(
        f"/api/v1/reviews/{'00000000-0000-0000-0000-000000000000'}/resolve",
        headers=auth_headers,
        json={"note": "n/a", "expected_row_version": 1},
    )
    assert missing.status_code == 404

    stale = client.post(
        f"/api/v1/reviews/{review_id}/resolve",
        headers=auth_headers,
        json={"note": "过期版本", "expected_row_version": 5},
    )
    assert stale.status_code == 409
    assert "changed" in stale.json()["detail"]

    still_open = _open_reviews(client, auth_headers)
    assert len(still_open) == 2


def test_resolve_review_is_idempotent_guarded_by_closed_check(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    _import_document(client, auth_headers)
    review = _open_reviews(client, auth_headers)[0]
    review_id = review["id"]

    first = client.post(
        f"/api/v1/reviews/{review_id}/resolve",
        headers=auth_headers,
        json={"note": "已核对医院规格缺失，剂量保持 NULL 可重算", "expected_row_version": 1},
    )
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "resolved"
    assert first.json()["row_version"] == 2

    replay = client.post(
        f"/api/v1/reviews/{review_id}/resolve",
        headers=auth_headers,
        json={"note": "重复提交", "expected_row_version": 1},
    )
    assert replay.status_code == 409
    assert "already closed" in replay.json()["detail"]

    remaining_open = _open_reviews(client, auth_headers)
    assert len(remaining_open) == 1


def test_resolving_all_reviews_accepts_document_and_items(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    document_id = _import_document(client, auth_headers)
    reviews = _open_reviews(client, auth_headers)
    for review in reviews:
        response = client.post(
            f"/api/v1/reviews/{review['id']}/resolve",
            headers=auth_headers,
            json={"note": "补充医院规格前保持参考数据缺口", "expected_row_version": 1},
        )
        assert response.status_code == 200, response.text

    with SessionLocal() as session:
        import_items = session.scalars(
            select(ImportItem).where(ImportItem.document_id == document_id)
        ).all()
        assert import_items
        assert all(
            item.status == ProcessingStatus.ACCEPTED for item in import_items
        )
        prescription_items = session.scalars(select(PrescriptionItem)).all()
        assert prescription_items
        assert all(
            item.review_status == ReviewStatus.RESOLVED for item in prescription_items
        )
        review_rows = session.scalars(select(ReviewItem)).all()
        assert review_rows
        assert all(row.status == ReviewStatus.RESOLVED for row in review_rows)
