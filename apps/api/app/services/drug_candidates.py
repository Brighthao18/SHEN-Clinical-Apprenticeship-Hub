"""Drug dictionary candidate lifecycle (Phase 6.2).

The dictionary is a gradually completed master-data store: the teacher's
common herb set is stable, so each new raw drug name is verified ONCE and
reused forever.

- Missing canonical mapping never blocks ingestion. A traceable raw name
  with no dictionary entry creates or reuses a PENDING candidate that
  accumulates usage_count / source documents / evidence.
- User confirmation (optionally edited, one-click accept of the AI
  suggestion, or batch) flips the candidate to VERIFIED and backfills ALL
  historical pending items carrying that raw name; the original
  raw_drug_name is preserved and every mutation is audited.
- DISABLED entries are known-bad and never resolve; AI suggestions never
  become VERIFIED automatically.

Candidates are reference data, not clinical facts: creating them for a
document that later fails validation is intentional (bootstrap completeness).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.base import utcnow
from app.models.domain import DrugDictionary, PrescriptionItem, ReviewItem, ReviewStatus
from app.services.audit import append_audit_event

STATUS_PENDING = "PENDING"
STATUS_VERIFIED = "VERIFIED"
STATUS_DISABLED = "DISABLED"


class DictionaryRejected(ValueError):
    """Raised when a dictionary mutation is invalid."""


def get_entry(session: Session, entry_id) -> DrugDictionary | None:
    return session.get(DrugDictionary, entry_id)


def find_candidate_by_raw_name(session: Session, raw_name: str) -> DrugDictionary | None:
    return session.scalar(
        select(DrugDictionary).where(DrugDictionary.candidate_raw_name == raw_name.strip())
    )


def upsert_candidate(
    session: Session,
    *,
    raw_name: str,
    suggested_canonical: str | None,
    suggested_processing: str | None,
    evidence_text: str | None,
    source_document_id,
) -> tuple[DrugDictionary, bool]:
    """Create or reuse the PENDING candidate for a printed raw drug name.

    The FIRST occurrence freezes the AI suggestion (users confirm or edit
    it once); later occurrences only accumulate usage. The AI suggestion is
    never authoritative and never auto-verifies.
    """

    raw = raw_name.strip()
    entry = find_candidate_by_raw_name(session, raw)
    if entry is not None:
        entry.usage_count = (entry.usage_count or 0) + 1
        entry.last_seen_at = utcnow()
        docs = list(entry.source_document_ids or [])
        if source_document_id and str(source_document_id) not in docs:
            docs.append(str(source_document_id))
        entry.source_document_ids = docs
        return entry, False

    entry = DrugDictionary(
        canonical_name=(suggested_canonical or raw),
        processing_method=suggested_processing,
        aliases=[raw],
        hospital_display_names=[raw],
        common_ocr_errors=[],
        dictionary_version=get_settings().drug_dictionary_version,
        effective_from=utcnow().date(),
        source="ai_candidate",
        status=STATUS_PENDING,
        candidate_raw_name=raw,
        representative_evidence=(evidence_text or "")[:500] or None,
        first_seen_at=utcnow(),
        last_seen_at=utcnow(),
        usage_count=1,
        source_document_ids=[str(source_document_id)] if source_document_id else [],
    )
    session.add(entry)
    session.flush()
    return entry, True


def verify_entry(
    session: Session,
    entry: DrugDictionary,
    *,
    canonical_name: str,
    processing_method: str | None,
    actor: str = "user",
) -> int:
    """Confirm a candidate (or re-confirm a verified entry) and backfill.

    All historical pending items carrying the candidate's raw name receive
    the verified canonical mapping; original raw_drug_name values are kept.
    """

    if entry.status == STATUS_DISABLED:
        raise DictionaryRejected("Disabled entries must be restored before verification")
    canonical = (canonical_name or "").strip()
    if not canonical:
        raise DictionaryRejected("canonical_name is required for verification")
    entry.status = STATUS_VERIFIED
    entry.canonical_name = canonical
    entry.processing_method = (processing_method or "").strip() or None
    raw = entry.candidate_raw_name
    if raw and raw not in (entry.hospital_display_names or []):
        entry.hospital_display_names = [*(entry.hospital_display_names or []), raw]
    if raw and raw not in (entry.aliases or []):
        entry.aliases = [*(entry.aliases or []), raw]

    items = session.scalars(
        select(PrescriptionItem).where(
            PrescriptionItem.raw_drug_name == raw,
            PrescriptionItem.normalization_status == "pending",
        )
    ).all() if raw else []
    for item in items:
        item.canonical_drug_name = canonical
        item.drug_dictionary_id = entry.id
        item.normalization_status = "normalized"
        item.normalization_version = get_settings().drug_dictionary_version
        item.review_status = ReviewStatus.RESOLVED
        for review in session.scalars(
            select(ReviewItem).where(
                ReviewItem.prescription_item_id == item.id,
                ReviewItem.issue_code == "UNKNOWN_DRUG",
                ReviewItem.status == ReviewStatus.OPEN,
            )
        ):
            review.status = ReviewStatus.RESOLVED
    append_audit_event(
        session,
        action="drug_dictionary.verified",
        entity_type="drug_dictionary",
        entity_id=entry.id,
        actor_type="user" if actor == "user" else actor,
        after_redacted={
            "candidate_raw_name": raw,
            "canonical_name": canonical,
            "backfilled_items": len(items),
            "dictionary_version": entry.dictionary_version,
        },
    )
    return len(items)


def disable_entry(session: Session, entry: DrugDictionary) -> None:
    """A known-bad entry never resolves; existing item rows keep history."""

    entry.status = STATUS_DISABLED
    append_audit_event(
        session,
        action="drug_dictionary.disabled",
        entity_type="drug_dictionary",
        entity_id=entry.id,
        actor_type="user",
        after_redacted={"canonical_name": entry.canonical_name},
    )


def restore_entry(session: Session, entry: DrugDictionary) -> None:
    """Re-open a disabled entry as PENDING so it can be confirmed again."""

    entry.status = STATUS_PENDING
    append_audit_event(
        session,
        action="drug_dictionary.restored",
        entity_type="drug_dictionary",
        entity_id=entry.id,
        actor_type="user",
        after_redacted={"canonical_name": entry.canonical_name},
    )


def merge_entries(session: Session, source: DrugDictionary, target: DrugDictionary) -> int:
    """Fold one entry into another; the source becomes DISABLED.

    The source's candidate raw name moves to the target so future
    occurrences of that raw name resolve/reuse against the survivor.
    Pending items under the source raw name are backfilled to the target.
    """

    if source.id == target.id:
        raise DictionaryRejected("Cannot merge an entry into itself")
    if target.status == STATUS_DISABLED:
        raise DictionaryRejected("Merge target must not be disabled")
    raw = source.candidate_raw_name
    target.hospital_display_names = list(
        dict.fromkeys(
            [*(target.hospital_display_names or []), *(source.hospital_display_names or [])]
        )
    )
    target.aliases = list(dict.fromkeys([*(target.aliases or []), *(source.aliases or [])]))
    target.usage_count = (target.usage_count or 0) + (source.usage_count or 0)
    target.source_document_ids = list(
        dict.fromkeys([*(target.source_document_ids or []), *(source.source_document_ids or [])])
    )
    if raw:
        target.candidate_raw_name = target.candidate_raw_name or raw
        source.candidate_raw_name = None
        pending = session.scalars(
            select(PrescriptionItem).where(
                PrescriptionItem.raw_drug_name == raw,
                PrescriptionItem.normalization_status == "pending",
            )
        ).all()
        for item in pending:
            item.canonical_drug_name = target.canonical_name
            item.drug_dictionary_id = target.id
            item.normalization_status = "normalized"
            item.normalization_version = get_settings().drug_dictionary_version
        source.usage_count = 0
    source.status = STATUS_DISABLED
    append_audit_event(
        session,
        action="drug_dictionary.merged",
        entity_type="drug_dictionary",
        entity_id=target.id,
        actor_type="user",
        after_redacted={"source_id": str(source.id), "target_id": str(target.id), "moved_raw": raw},
    )
    return 0


def list_entries(session: Session, *, status: str | None, q: str | None) -> list[dict[str, Any]]:
    statement = select(DrugDictionary).order_by(DrugDictionary.usage_count.desc())
    if status:
        statement = statement.where(DrugDictionary.status == status)
    entries = session.scalars(statement).all()
    needle = (q or "").strip()
    # 影响范围计数：各用一次分组查询，帮助用户判断确认/停用/合并的价值。
    # linked = 已通过该词条解析的处方条目；pending_raw = 同名待标准化、
    # 确认后可回填的处方条目。
    linked_counts: dict[Any, int] = dict(
        session.execute(
            select(PrescriptionItem.drug_dictionary_id, func.count(PrescriptionItem.id))
            .where(PrescriptionItem.drug_dictionary_id.is_not(None))
            .group_by(PrescriptionItem.drug_dictionary_id)
        ).all()
    )
    pending_raw_counts: dict[str, int] = dict(
        session.execute(
            select(PrescriptionItem.raw_drug_name, func.count(PrescriptionItem.id))
            .where(PrescriptionItem.normalization_status == "pending")
            .group_by(PrescriptionItem.raw_drug_name)
        ).all()
    )
    out = []
    for entry in entries:
        haystack = " ".join(
            filter(None, [entry.canonical_name, entry.candidate_raw_name, *(entry.aliases or [])])
        )
        if needle and needle not in haystack:
            continue
        out.append(
            _serialize(
                entry,
                linked_count=linked_counts.get(entry.id, 0),
                pending_raw_count=(
                    pending_raw_counts.get(entry.candidate_raw_name, 0)
                    if entry.candidate_raw_name
                    else 0
                ),
            )
        )
    return out


def _serialize(
    entry: DrugDictionary,
    *,
    linked_count: int = 0,
    pending_raw_count: int = 0,
) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "status": entry.status,
        "canonical_name": entry.canonical_name,
        "processing_method": entry.processing_method,
        "candidate_raw_name": entry.candidate_raw_name,
        "aliases": entry.aliases or [],
        "hospital_display_names": entry.hospital_display_names or [],
        "usage_count": entry.usage_count or 0,
        "first_seen_at": entry.first_seen_at.isoformat() if entry.first_seen_at else None,
        "last_seen_at": entry.last_seen_at.isoformat() if entry.last_seen_at else None,
        "representative_evidence": entry.representative_evidence,
        "source": entry.source,
        "source_documents": len(entry.source_document_ids or []),
        "dictionary_version": entry.dictionary_version,
        "linked_item_count": linked_count,
        "pending_raw_item_count": pending_raw_count,
    }
