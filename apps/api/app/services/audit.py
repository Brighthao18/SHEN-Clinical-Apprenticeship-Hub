from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.models.domain import AuditEvent


def append_audit_event(
    session: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID,
    patient_id: uuid.UUID | None = None,
    actor_type: str = "system",
    before_redacted: dict[str, Any] | None = None,
    after_redacted: dict[str, Any] | None = None,
    correlation_id: uuid.UUID | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_type=actor_type,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        patient_id=patient_id,
        before_redacted=before_redacted,
        after_redacted=after_redacted,
        correlation_id=correlation_id or uuid.uuid4(),
    )
    session.add(event)
    return event
