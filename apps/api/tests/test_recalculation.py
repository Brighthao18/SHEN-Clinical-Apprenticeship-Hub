from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.domain import AuditEvent, PrescriptionItem
from app.models.enums import DoseScope
from app.services.recalculation import DoseScopeRecalculationService

FIXTURE = Path(__file__).parent / "fixtures" / "jnu_outpatient_v1.txt"


def test_jnu_unknown_dose_scope_recalculation_is_previewable_audited_and_reversible(
    client, auth_headers, monkeypatch
) -> None:
    # The recalculation repair tool only touches physician-record rows; run
    # the import through the deterministic fallback parser to produce them.
    monkeypatch.setattr(get_settings(), "ai_extraction_enabled", False, raising=False)
    response = client.post(
        "/api/v1/imports",
        headers=auth_headers,
        files={"files": ("visit.jpg", b"recalculation-fixture", "image/jpeg")},
        data={"mock_ocr_text": FIXTURE.read_text(encoding="utf-8")},
    )
    assert response.status_code == 202, response.text

    with SessionLocal() as session:
        items = list(session.scalars(select(PrescriptionItem).order_by(PrescriptionItem.id)))
        original_text_by_id = {item.id: item.original_text for item in items}
        assert len(items) == 11
        for item in items:
            item.dose_scope = DoseScope.UNKNOWN
        session.commit()

        service = DoseScopeRecalculationService(session)
        preview = service.preview()
        assert preview.eligible_count == 11
        assert preview.changed_count == 0
        assert preview.correlation_id is None

        applied = service.apply()
        assert applied.changed_count == 11
        assert applied.correlation_id is not None
        assert (
            session.scalar(
                select(func.count(PrescriptionItem.id)).where(
                    PrescriptionItem.dose_scope == DoseScope.PER_DECOCTION
                )
            )
            == 11
        )
        assert (
            session.scalar(
                select(func.count(AuditEvent.id)).where(
                    AuditEvent.correlation_id == applied.correlation_id,
                    AuditEvent.action == "derived.dose_scope.recalculated",
                )
            )
            == 11
        )
        assert {
            item.id: item.original_text for item in session.scalars(select(PrescriptionItem))
        } == original_text_by_id

        rolled_back = service.rollback(applied.correlation_id)
        assert rolled_back.changed_count == 11
        assert rolled_back.correlation_id is not None
        assert (
            session.scalar(
                select(func.count(PrescriptionItem.id)).where(
                    PrescriptionItem.dose_scope == DoseScope.UNKNOWN
                )
            )
            == 11
        )
