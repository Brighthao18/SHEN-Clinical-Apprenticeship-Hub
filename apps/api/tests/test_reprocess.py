"""Explicit unresolved-reprocess service and CLI entry tests.

Reprocessing is explicit-command-only (never automatic): dry-run first,
--apply to execute, stable IDs/counts only. The CLI prints JSON without
clinical content.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid

import pytest
from sqlalchemy import select

from app.models.domain import Document, DocumentType, UnresolvedIngestion
from app.models.enums import ProcessingStatus
from app.services.reprocess import build_parser, list_unresolved, main, reprocess_unresolved

# The suite defaults to in-memory SQLite (no FK enforcement); an externally
# provided DATABASE_URL runs it against a real engine such as PostgreSQL.
IS_SQLITE = os.environ.get("DATABASE_URL", "sqlite+pysqlite:///:memory:").startswith("sqlite")


def _document_for(label: str) -> Document:
    digest = hashlib.sha256(label.encode()).hexdigest()
    return Document(
        id=uuid.uuid5(uuid.NAMESPACE_URL, label),
        original_filename=f"{label}.jpg",
        original_file_path=f"synthetic/{digest}.jpg",
        sha256=digest,
        byte_size=10,
        mime_type="image/jpeg",
        document_type=DocumentType.OUTPATIENT_RECORD,
        processing_status=ProcessingStatus.UNRESOLVED,
        failure_code="AI_EXTRACTION_PROVIDER_FAILED",
        # Keep replays off the OCR/storage path: this synthetic document has
        # no stored file, so the pipeline must decide from the OCR text alone
        # (unknown template → UNRESOLVED, no storage access).
        ocr_text="中药处方笺\nRp\n没有任何字段",
    )


def _unresolved_row(label: str, *, template: str = "UNKNOWN") -> UnresolvedIngestion:
    return UnresolvedIngestion(
        id=uuid.uuid4(),
        document_id=uuid.uuid5(uuid.NAMESPACE_URL, label),
        template=template,
        failure_stage=ProcessingStatus.PARSING,
        failure_reason_code="AI_EXTRACTION_PROVIDER_FAILED",
        reason_category="ai_provider",
        critical_fields_affected=[],
        parser_name="ai-clinical",
        parser_version="ai-clinical/test",
        normalizer_version=None,
        validation_policy_version="test",
        ocr_provider="mock",
        retry_count=1,
    )


def test_list_unresolved_summarizes_and_filters(db_session) -> None:
    document = _document_for("reprocess-list")
    row = _unresolved_row("reprocess-list")
    db_session.add_all([document, row])
    db_session.commit()

    rows = list_unresolved(db_session)
    assert len(rows) == 1
    assert rows[0]["document_id"] == str(row.document_id)
    assert rows[0]["failure_reason_code"] == "AI_EXTRACTION_PROVIDER_FAILED"
    assert rows[0]["retry_count"] == 1

    filtered = list_unresolved(db_session, template="CURRENT_NOT_A_TEMPLATE")
    assert filtered == []
    version_filtered = list_unresolved(db_session, parser_version="ai-clinical/test")
    assert len(version_filtered) == 1


@pytest.mark.skipif(
    not IS_SQLITE,
    reason=(
        "An orphaned ledger row (its document vanished) cannot exist under real "
        "foreign-key enforcement; the document_missing branch is drift-only "
        "protection exercised on the SQLite test engine."
    ),
)
def test_reprocess_apply_counts_missing_document(db_session) -> None:
    """A ledger row whose document row vanished is counted, never crashes."""

    db_session.add(_unresolved_row("reprocess-missing-doc"))
    db_session.commit()

    plan = reprocess_unresolved(db_session, apply=True)
    assert plan["status"] == "reprocess_applied"
    assert plan["outcomes"] == {"document_missing": 1}
    assert plan["replayed"] == []


def test_reprocess_apply_closes_stale_reviews_then_replays(db_session) -> None:
    """An apply replay re-runs the pipeline; the deterministic mock still
    fails closed for unannotated OCR, so the outcome stays UNRESOLVED."""

    from app.models.domain import ReviewItem
    from app.models.enums import ReviewStatus

    digest = hashlib.sha256(b"reprocess-replay").hexdigest()
    document = Document(
        id=uuid.uuid5(uuid.NAMESPACE_URL, "reprocess-replay"),
        original_filename="replay.jpg",
        original_file_path=f"synthetic/{digest}.jpg",
        sha256=digest,
        byte_size=10,
        mime_type="image/jpeg",
        document_type=DocumentType.OUTPATIENT_RECORD,
        processing_status=ProcessingStatus.UNRESOLVED,
        failure_code="AI_EXTRACTION_PROVIDER_FAILED",
        ocr_text="中药处方笺\nRp\n没有任何字段",
    )
    row = _unresolved_row("reprocess-replay")
    row.document_id = document.id
    review = ReviewItem(
        document_id=document.id,
        encounter_id=None,
        issue_code="AI_EXTRACTION_PROVIDER_FAILED",
        field_path="document",
        severity="error",
        blocking=True,
        observed_value={"reason": "AI_EXTRACTION_PROVIDER_FAILED"},
        status=ReviewStatus.OPEN,
    )
    db_session.add_all([document, row, review])
    db_session.commit()

    plan = reprocess_unresolved(db_session, apply=True)
    assert plan["outcomes"] == {"UNRESOLVED": 1}
    replayed = plan["replayed"][0]
    assert replayed["document_id"] == str(document.id)
    assert replayed["outcome_status"] == "UNRESOLVED"
    assert replayed["encounter_id"] is None

    db_session.expire_all()
    stale = db_session.scalar(select(ReviewItem))
    assert stale is not None
    assert stale.status == ReviewStatus.RESOLVED
    assert "reprocess" in stale.resolution_note
    assert db_session.get(Document, document.id).failure_code == "AI_EXTRACTION_PROVIDER_FAILED"


def test_cli_list_and_reprocess_print_json(db_session, capsys) -> None:
    document = _document_for("reprocess-cli")
    row = _unresolved_row("reprocess-cli")
    db_session.add_all([document, row])
    db_session.commit()

    assert main(["list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["status"] == "unresolved_listed"
    assert len(listed["rows"]) == 1

    assert main(["reprocess"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["status"] == "reprocess_dry_run"

    assert main(["reprocess", "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "reprocess_applied"


def test_cli_parser_accepts_filters() -> None:
    args = build_parser().parse_args(
        ["list", "--template", "UNKNOWN", "--parser-version", "ai-clinical/test"]
    )
    assert args.command == "list"
    assert args.template == "UNKNOWN"
    assert args.parser_version == "ai-clinical/test"
    assert args.apply is False
