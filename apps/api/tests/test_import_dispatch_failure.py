"""Upload dispatch-failure handling: committed rows must never strand in QUEUED.

A broker outage between the upload commit and the Celery publish used to
return 500 with documents stuck in QUEUED that no worker would ever pick up
and that the retry endpoint rejected. The upload route now records the honest
retryable terminal (FAILED + DISPATCH_FAILED) per document, keeps dispatching
the remaining files, and the retry endpoint accepts QUEUED for legacy
stranded rows.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db.session import SessionLocal
from app.models.domain import Document, ImportItem


def test_upload_marks_dispatch_failure_and_retry_accepts_it(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    from app.api.routes import imports as imports_module

    def broken_dispatch(document_id: uuid.UUID):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(imports_module, "dispatch_document", broken_dispatch)

    response = client.post(
        "/api/v1/imports",
        headers=auth_headers,
        files={"files": ("visit.jpg", b"dispatch-failure-evidence", "image/jpeg")},
        data={"mock_ocr_text": "无关内容：投递在管线运行前就失败了"},
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "FAILED"
    assert len(payload["items"]) == 1
    item = payload["items"][0]
    assert item["status"] == "FAILED"
    assert item["error_code"] == "DISPATCH_FAILED"

    with SessionLocal() as session:
        document = session.get(Document, uuid.UUID(item["document_id"]))
        assert document is not None
        assert document.processing_status.value == "FAILED"
        assert document.failure_code == "DISPATCH_FAILED"

    # The retry entry point accepts the honest dispatch-failure terminal.
    retry = client.post(f"/api/v1/documents/{item['document_id']}/retry", headers=auth_headers)
    assert retry.status_code == 200, retry.text


def test_mid_batch_dispatch_failure_only_marks_affected_documents(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    from app.api.routes import imports as imports_module
    from app.core.config import get_settings

    # Two files need the mock single-file guard bypassed; dispatch is stubbed
    # below, so no OCR provider is ever resolved.
    real_settings = get_settings()

    class _Shim:
        ocr_provider = "test-shim"
        max_upload_bytes = real_settings.max_upload_bytes

    monkeypatch.setattr(imports_module, "get_settings", lambda: _Shim)

    calls: list[uuid.UUID] = []

    def flaky_dispatch(document_id: uuid.UUID):
        calls.append(document_id)
        if len(calls) == 2:
            raise RuntimeError("broker died mid-batch")
        return None  # first message accepted by the broker

    monkeypatch.setattr(imports_module, "dispatch_document", flaky_dispatch)

    response = client.post(
        "/api/v1/imports",
        headers=auth_headers,
        files=[
            ("files", ("a.jpg", b"first-file-bytes", "image/jpeg")),
            ("files", ("b.jpg", b"second-file-bytes", "image/jpeg")),
        ],
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert len(payload["items"]) == 2

    first, second = payload["items"]
    # First document was accepted by the broker: it stays QUEUED (a worker
    # would pick it up), which is honest.
    assert first["status"] == "QUEUED"
    assert first["error_code"] is None
    # Second document's dispatch failed: honest retryable terminal.
    assert second["status"] == "FAILED"
    assert second["error_code"] == "DISPATCH_FAILED"
    # The batch reflects the stranded failure instead of a silent 500.
    assert payload["status"] == "QUEUED"
    assert payload["failed_items"] == 1

    with SessionLocal() as session:
        statuses = dict(
            session.execute(
                select(ImportItem.ordinal, ImportItem.status).where(
                    ImportItem.batch_id == uuid.UUID(payload["id"])
                )
            ).all()
        )
        assert statuses[1] == "QUEUED"
        assert statuses[2] == "FAILED"

    # A legacy row stranded in QUEUED can be re-enqueued through the retry
    # endpoint instead of requiring SQL surgery.
    retry = client.post(f"/api/v1/documents/{first['document_id']}/retry", headers=auth_headers)
    assert retry.status_code == 200, retry.text


def test_every_batch_dispatch_failure_is_recorded(
    client: TestClient, auth_headers: dict[str, str], monkeypatch
) -> None:
    """A second failure's expire_all must not discard the first marking."""

    from app.api.routes import imports as imports_module
    from app.core.config import get_settings

    real_settings = get_settings()

    class _Shim:
        ocr_provider = "test-shim"
        max_upload_bytes = real_settings.max_upload_bytes

    monkeypatch.setattr(imports_module, "get_settings", lambda: _Shim)

    def broken_dispatch(document_id: uuid.UUID):
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(imports_module, "dispatch_document", broken_dispatch)

    response = client.post(
        "/api/v1/imports",
        headers=auth_headers,
        files=[
            ("files", ("a.jpg", b"first-failed-bytes", "image/jpeg")),
            ("files", ("b.jpg", b"second-failed-bytes", "image/jpeg")),
        ],
    )
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["status"] == "FAILED"
    assert payload["failed_items"] == 2
    for item in payload["items"]:
        assert item["status"] == "FAILED"
        assert item["error_code"] == "DISPATCH_FAILED"

    with SessionLocal() as session:
        rows = list(
            session.execute(
                select(ImportItem.status, ImportItem.error_code).where(
                    ImportItem.batch_id == uuid.UUID(payload["id"])
                )
            ).all()
        )
        assert {str(status) for status, _ in rows} == {"FAILED"}
        assert {code for _, code in rows} == {"DISPATCH_FAILED"}
