"""One-time historical provenance correction (explicit, idempotent, dry-run first).

Background: before the Goal Mode hardening fix in ``app/services/pipeline.py``,
encounters ingested by the ClinicalAIExtractor stored the encounter narrative
(``Encounter.raw_source_type``) and every symptom row
(``SymptomObservation.source_type``) as ``physician_record`` even though the
AI extractor — not the physician's chart — produced them. Diagnoses and
prescriptions already carried the correct conditional ``ai_normalized`` label.

This script re-labels ONLY those provenance columns to ``ai_normalized`` for
encounters whose primary document was structurally produced by the AI extractor
(``Document.parser_version`` starts with ``ai-clinical/``).

Guarantees:
- dry-run by default; ``--apply`` is the explicit mutation switch;
- only ``raw_source_type`` / ``source_type`` provenance values are touched;
- no clinical text, dose, identity, dictionary, or OCR data is modified;
- no OCR or AI call is made; nothing is re-imported or re-processed;
- idempotent: after a successful apply the same run reports 0 affected rows;
- apply writes an ``audit_events`` row listing corrected record IDs (no PII);
- encounter ``row_version`` is incremented so the correction is auditable
  versioned data, never a silent mutation.

Usage (host shell, development):
    docker compose -f compose.yaml exec -T api python - \
        < scripts/fix_historical_ai_provenance.py            # dry-run
    docker compose -f compose.yaml exec -T api python - --apply \
        < scripts/fix_historical_ai_provenance.py            # explicit apply
Production: same with ``-f compose.yaml -f compose.production.yaml
--env-file .env.production.local``.

Output is counts and UUIDs only; no patient names, MRNs, or clinical text.
"""

from __future__ import annotations

import argparse
import sys
import uuid

from app.db.base import utcnow
from app.db.session import SessionLocal
from app.models.domain import Document, Encounter
from app.models.enums import SourceType
from app.services.audit import append_audit_event
from sqlalchemy import select

AI_PARSER_PREFIX = "ai-clinical/"


def find_affected(session) -> tuple[list[Encounter], int]:
    """AI-era encounters still labelled physician_record, and their stale symptom rows."""

    documents = session.scalars(
        select(Document).where(
            Document.parser_version.like(f"{AI_PARSER_PREFIX}%"),
        )
    ).all()
    document_ids = {document.id for document in documents}
    encounters: list[Encounter] = []
    stale_symptom_count = 0
    for encounter in session.scalars(select(Encounter)).all():
        if encounter.primary_document_id not in document_ids:
            continue
        if encounter.raw_source_type != SourceType.PHYSICIAN_RECORD:
            continue
        encounters.append(encounter)
        stale_symptom_count += sum(
            1
            for symptom in encounter.symptoms
            if symptom.source_type == SourceType.PHYSICIAN_RECORD
        )
    return encounters, stale_symptom_count


def report(session, *, applying: bool) -> int:
    encounters, stale_symptoms = find_affected(session)
    mode = "APPLY" if applying else "DRY-RUN"
    print(f"[{mode}] affected encounters: {len(encounters)}")
    print(f"[{mode}] stale symptom rows inside them: {stale_symptoms}")
    for encounter in encounters:
        print(
            f"[{mode}] encounter_id={encounter.id} "
            f"document_id={encounter.primary_document_id} "
            f"row_version={encounter.row_version}"
        )
    if not applying:
        print(
            "[DRY-RUN] no changes were made. Re-run with --apply to correct "
            "the provenance labels (ai_normalized)."
        )
        return 0

    corrected_encounter_ids: list[str] = []
    corrected_symptom_ids: list[str] = []
    for encounter in encounters:
        encounter.raw_source_type = SourceType.AI_NORMALIZED
        encounter.row_version += 1
        corrected_encounter_ids.append(str(encounter.id))
        for symptom in encounter.symptoms:
            if symptom.source_type == SourceType.PHYSICIAN_RECORD:
                symptom.source_type = SourceType.AI_NORMALIZED
                corrected_symptom_ids.append(str(symptom.id))

    append_audit_event(
        session,
        action="historical_provenance_correction.applied",
        entity_type="encounter",
        entity_id=uuid.UUID(corrected_encounter_ids[0]) if corrected_encounter_ids else uuid.uuid4(),
        patient_id=None,
        actor_type="system",
        after_redacted={
            "correction": "physician_record->ai_normalized",
            "scope": "encounter.raw_source_type + symptom_observations.source_type only",
            "encounter_ids": corrected_encounter_ids,
            "symptom_row_count": len(corrected_symptom_ids),
            "symptom_ids": corrected_symptom_ids,
            "detection": f"document.parser_version startswith {AI_PARSER_PREFIX}",
            "executed_at": utcnow().isoformat(),
        },
    )
    session.commit()
    print(
        f"[APPLY] corrected {len(corrected_encounter_ids)} encounter(s) and "
        f"{len(corrected_symptom_ids)} symptom row(s); audit event written."
    )
    # Idempotency proof: the same predicate now matches nothing.
    remaining, _ = find_affected(session)
    print(f"[APPLY] rows still matching the stale predicate afterwards: {len(remaining)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="explicitly apply the correction (default is a no-write dry-run)",
    )
    args = parser.parse_args()
    with SessionLocal() as session:
        return report(session, applying=args.apply)


if __name__ == "__main__":
    sys.exit(main())
