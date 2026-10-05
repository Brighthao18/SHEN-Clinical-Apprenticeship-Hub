from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import AuditEvent, Document, Encounter, Prescription, PrescriptionItem
from app.models.enums import DoseScope, SourceType
from app.services.audit import append_audit_event
from app.services.parsers.jnu import JNUHospitalOutpatientParser

JNU_DOSE_SCOPE_RULE_VERSION = "jnu-per-decoction-20260829"


@dataclass(frozen=True)
class RecalculationResult:
    mode: str
    eligible_count: int
    changed_count: int
    correlation_id: uuid.UUID | None


class DoseScopeRecalculationService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def preview(self) -> RecalculationResult:
        return RecalculationResult(
            mode="preview",
            eligible_count=len(self._eligible_rows()),
            changed_count=0,
            correlation_id=None,
        )

    def apply(self, correlation_id: uuid.UUID | None = None) -> RecalculationResult:
        rows = self._eligible_rows()
        run_id = correlation_id or uuid.uuid4()
        for item, encounter in rows:
            item.dose_scope = DoseScope.PER_DECOCTION
            append_audit_event(
                self.session,
                action="derived.dose_scope.recalculated",
                entity_type="prescription_item",
                entity_id=item.id,
                patient_id=encounter.patient_id,
                actor_type="system_recalculation",
                before_redacted={"dose_scope": DoseScope.UNKNOWN.value},
                after_redacted={
                    "dose_scope": DoseScope.PER_DECOCTION.value,
                    "rule_version": JNU_DOSE_SCOPE_RULE_VERSION,
                },
                correlation_id=run_id,
            )
        self.session.commit()
        return RecalculationResult(
            mode="apply",
            eligible_count=len(rows),
            changed_count=len(rows),
            correlation_id=run_id,
        )

    def rollback(self, original_correlation_id: uuid.UUID) -> RecalculationResult:
        events = list(
            self.session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.correlation_id == original_correlation_id,
                    AuditEvent.action == "derived.dose_scope.recalculated",
                    AuditEvent.entity_type == "prescription_item",
                )
                .order_by(AuditEvent.created_at, AuditEvent.id)
            )
        )
        if not events:
            raise ValueError("No dose-scope recalculation run found for that correlation ID")

        items: list[tuple[PrescriptionItem, AuditEvent]] = []
        for event in events:
            item = self.session.get(PrescriptionItem, event.entity_id)
            if item is None or item.dose_scope != DoseScope.PER_DECOCTION:
                raise ValueError("Recalculated item no longer has the expected per-decoction scope")
            later_event = self.session.scalar(
                select(AuditEvent.id)
                .where(
                    AuditEvent.entity_type == "prescription_item",
                    AuditEvent.entity_id == item.id,
                    AuditEvent.created_at > event.created_at,
                    AuditEvent.action.in_(
                        [
                            "derived.dose_scope.recalculated",
                            "derived.dose_scope.recalculation_rolled_back",
                        ]
                    ),
                )
                .limit(1)
            )
            if later_event is not None:
                raise ValueError("A later dose-scope change prevents safe rollback")
            items.append((item, event))

        rollback_id = uuid.uuid4()
        for item, event in items:
            item.dose_scope = DoseScope.UNKNOWN
            append_audit_event(
                self.session,
                action="derived.dose_scope.recalculation_rolled_back",
                entity_type="prescription_item",
                entity_id=item.id,
                patient_id=event.patient_id,
                actor_type="system_recalculation",
                before_redacted={"dose_scope": DoseScope.PER_DECOCTION.value},
                after_redacted={
                    "dose_scope": DoseScope.UNKNOWN.value,
                    "rolled_back_correlation_id": str(original_correlation_id),
                },
                correlation_id=rollback_id,
            )
        self.session.commit()
        return RecalculationResult(
            mode="rollback",
            eligible_count=len(items),
            changed_count=len(items),
            correlation_id=rollback_id,
        )

    def _eligible_rows(self) -> list[tuple[PrescriptionItem, Encounter]]:
        statement = (
            select(PrescriptionItem, Encounter)
            .join(Prescription, Prescription.id == PrescriptionItem.prescription_id)
            .join(Encounter, Encounter.id == Prescription.encounter_id)
            .join(Document, Document.id == Encounter.primary_document_id)
            .where(
                Document.parser_template == JNUHospitalOutpatientParser.template,
                Prescription.source_type == SourceType.PHYSICIAN_RECORD,
                PrescriptionItem.dose_scope == DoseScope.UNKNOWN,
                PrescriptionItem.normalized_package_unit == "package",
            )
            .order_by(PrescriptionItem.id)
        )
        return [(item, encounter) for item, encounter in self.session.execute(statement)]
