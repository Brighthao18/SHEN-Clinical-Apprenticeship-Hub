from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import CurrentUser
from app.db.session import get_db
from app.models.domain import UnresolvedIngestion
from app.schemas.api import UnresolvedIngestionRead

router = APIRouter(prefix="/ingestion", tags=["ingestion"])
DbSession = Annotated[Session, Depends(get_db)]


@router.get("/unresolved", response_model=list[UnresolvedIngestionRead])
def list_unresolved_ingestions(
    user: CurrentUser,
    db: DbSession,
    template: Annotated[str | None, Query(max_length=100)] = None,
    include_resolved: bool = False,
) -> list[UnresolvedIngestion]:
    """List the unresolved-ingestion retry ledger.

    This is not a human work queue: unresolved documents expect no manual
    action. Rows carry stable IDs and failure metadata only, and are replayed
    exclusively through the explicit ``scripts/reprocess_unresolved.py``
    command after a parser/policy upgrade.
    """

    statement = select(UnresolvedIngestion).order_by(UnresolvedIngestion.last_attempt_at.desc())
    if template:
        statement = statement.where(UnresolvedIngestion.template == template)
    if not include_resolved:
        statement = statement.where(UnresolvedIngestion.resolved_at.is_(None))
    return list(db.scalars(statement))
