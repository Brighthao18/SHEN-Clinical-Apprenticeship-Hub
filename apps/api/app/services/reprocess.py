"""Explicit unresolved-ingestion reprocessing.

Never triggered automatically by application startup. A human runs the
``scripts/reprocess_unresolved.py`` wrapper (dry-run first, then ``--apply``)
after a parser/policy upgrade; only documents currently sitting in the
unresolved ledger are replayed. Output contains stable IDs and counts only.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models.domain import Document, ProcessingJob, ReviewItem, UnresolvedIngestion
from app.models.enums import JobStatus, ProcessingStatus, ReviewStatus
from app.services.pipeline import PipelineService


def _selected_rows(
    session: Session,
    *,
    template: str | None = None,
    parser_version: str | None = None,
) -> list[UnresolvedIngestion]:
    statement = select(UnresolvedIngestion).where(UnresolvedIngestion.resolved_at.is_(None))
    if template:
        statement = statement.where(UnresolvedIngestion.template == template)
    if parser_version:
        statement = statement.where(UnresolvedIngestion.parser_version == parser_version)
    return list(session.scalars(statement.order_by(UnresolvedIngestion.last_attempt_at.desc())))


def _row_summary(row: UnresolvedIngestion) -> dict[str, Any]:
    return {
        "document_id": str(row.document_id),
        "template": row.template,
        "failure_reason_code": row.failure_reason_code,
        "parser_version": row.parser_version,
        "validation_policy_version": row.validation_policy_version,
        "retry_count": row.retry_count,
        "last_attempt_at": row.last_attempt_at.isoformat() if row.last_attempt_at else None,
    }


def list_unresolved(
    session: Session,
    *,
    template: str | None = None,
    parser_version: str | None = None,
) -> list[dict[str, Any]]:
    return [
        _row_summary(row)
        for row in _selected_rows(session, template=template, parser_version=parser_version)
    ]


def reprocess_unresolved(
    session: Session,
    *,
    template: str | None = None,
    parser_version: str | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    rows = _selected_rows(session, template=template, parser_version=parser_version)
    plan: dict[str, Any] = {
        "status": "reprocess_dry_run" if not apply else "reprocess_applied",
        "planned_document_count": len(rows),
        "planned": [_row_summary(row) for row in rows],
    }
    if not apply:
        return plan

    outcomes: dict[str, int] = {}
    replayed: list[dict[str, Any]] = []
    for row in rows:
        document = session.get(Document, row.document_id)
        if document is None:
            outcomes["document_missing"] = outcomes.get("document_missing", 0) + 1
            continue
        # Close stale open review diagnostics from the failed attempt so the
        # replay starts from a clean policy state.
        for review in session.scalars(
            select(ReviewItem).where(
                ReviewItem.document_id == document.id,
                ReviewItem.status == ReviewStatus.OPEN,
            )
        ).all():
            review.status = ReviewStatus.RESOLVED
            review.resolution_note = "closed for explicit parser-upgrade reprocess"
            review.resolved_at = utcnow()
        document.processing_status = ProcessingStatus.QUEUED
        document.failure_code = None
        for job in session.scalars(
            select(ProcessingJob).where(ProcessingJob.document_id == document.id)
        ).all():
            job.status = JobStatus.RETRY_SCHEDULED
            job.next_retry_at = utcnow()
        session.flush()
        result = PipelineService(session).process(document.id)
        outcomes[result.status.value] = outcomes.get(result.status.value, 0) + 1
        replayed.append(
            {
                "document_id": str(document.id),
                "outcome_status": result.status.value,
                "encounter_id": str(result.encounter_id) if result.encounter_id else None,
            }
        )
    plan["replayed"] = replayed
    plan["outcomes"] = outcomes
    return plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.services.reprocess",
        description="List or explicitly replay unresolved ingestions (IDs/counts only)",
    )
    parser.add_argument("command", choices=["list", "reprocess"])
    parser.add_argument("--template", default=None, help="Filter by template family")
    parser.add_argument("--parser-version", default=None, help="Filter by recorded parser version")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually replay; without this flag the command is a dry run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with SessionLocal() as session:
        if args.command == "list":
            payload: dict[str, Any] = {
                "status": "unresolved_listed",
                "rows": list_unresolved(
                    session, template=args.template, parser_version=args.parser_version
                ),
            }
        else:
            payload = reprocess_unresolved(
                session,
                template=args.template,
                parser_version=args.parser_version,
                apply=args.apply,
            )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
