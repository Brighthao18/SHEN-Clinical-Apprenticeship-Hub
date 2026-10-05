from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.models.domain import Encounter, PrescriptionItem
from app.models.enums import DoseScope

PRESCRIPTION_DIFF_ALGORITHM_VERSION = "1.0.0"


class PrescriptionChange(StrEnum):
    ADDED = "ADDED"
    REMOVED = "REMOVED"
    DOSE_INCREASED = "DOSE_INCREASED"
    DOSE_DECREASED = "DOSE_DECREASED"
    PROCESSING_CHANGED = "PROCESSING_CHANGED"
    UNCHANGED = "UNCHANGED"
    NORMALIZATION_PENDING = "NORMALIZATION_PENDING"


class DoseChange(StrEnum):
    INCREASED = "INCREASED"
    DECREASED = "DECREASED"
    UNCHANGED = "UNCHANGED"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class IdentityBasis(StrEnum):
    VERIFIED_CANONICAL = "verified_canonical"
    RAW_EXACT = "raw_exact"


class AmbiguousPrescriptionDiffError(RuntimeError):
    """Raised when duplicate identities make a one-to-one comparison unsafe."""


@dataclass(frozen=True)
class _ItemRef:
    item: PrescriptionItem
    encounter: Encounter

    @property
    def pending(self) -> bool:
        return (
            self.item.normalization_status.casefold() != "normalized"
            or not self.item.canonical_drug_name
        )

    @property
    def canonical_key(self) -> str | None:
        if self.pending or not self.item.canonical_drug_name:
            return None
        return _identity_key(self.item.canonical_drug_name)

    @property
    def raw_key(self) -> str:
        return _identity_key(self.item.raw_drug_name)

    @property
    def processing_key(self) -> str:
        return _identity_key(self.item.processing_method or "")

    @property
    def comparable_dose(self) -> Decimal | None:
        if self.item.dose_scope != DoseScope.PER_DECOCTION:
            return None
        return self.item.calculated_dose_g


def _identity_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"\s+", "", normalized).strip()


def _decimal(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


def _flatten(encounter: Encounter) -> list[_ItemRef]:
    refs: list[_ItemRef] = []
    for prescription in sorted(encounter.prescriptions, key=lambda value: str(value.id)):
        for item in sorted(
            prescription.items, key=lambda value: (value.line_number, str(value.id))
        ):
            refs.append(_ItemRef(item=item, encounter=encounter))
    return refs


def _remove_ref(refs: list[_ItemRef], target: _ItemRef) -> None:
    refs.remove(target)


def _pair_canonical_group(
    previous: list[_ItemRef], current: list[_ItemRef]
) -> list[tuple[_ItemRef, _ItemRef]]:
    """Pair one canonical identity without guessing between processing variants."""

    pairs: list[tuple[_ItemRef, _ItemRef]] = []
    previous_remaining = list(previous)
    current_remaining = list(current)

    previous_by_processing: dict[str, list[_ItemRef]] = defaultdict(list)
    current_by_processing: dict[str, list[_ItemRef]] = defaultdict(list)
    for ref in previous_remaining:
        previous_by_processing[ref.processing_key].append(ref)
    for ref in current_remaining:
        current_by_processing[ref.processing_key].append(ref)

    for processing_key in sorted(previous_by_processing.keys() & current_by_processing.keys()):
        previous_same = previous_by_processing[processing_key]
        current_same = current_by_processing[processing_key]
        if len(previous_same) == len(current_same) == 1:
            previous_ref = previous_same[0]
            current_ref = current_same[0]
            pairs.append((previous_ref, current_ref))
            _remove_ref(previous_remaining, previous_ref)
            _remove_ref(current_remaining, current_ref)

    if previous_remaining and current_remaining:
        if len(previous_remaining) == len(current_remaining) == 1:
            pairs.append((previous_remaining.pop(), current_remaining.pop()))
        else:
            raise AmbiguousPrescriptionDiffError("canonical identity is not uniquely pairable")

    previous[:] = previous_remaining
    current[:] = current_remaining
    return pairs


def _match_items(
    previous_refs: list[_ItemRef], current_refs: list[_ItemRef]
) -> tuple[
    list[tuple[_ItemRef, _ItemRef, IdentityBasis]], list[_ItemRef], list[_ItemRef]
]:
    pairs: list[tuple[_ItemRef, _ItemRef, IdentityBasis]] = []
    previous_remaining = list(previous_refs)
    current_remaining = list(current_refs)

    previous_by_canonical: dict[str, list[_ItemRef]] = defaultdict(list)
    current_by_canonical: dict[str, list[_ItemRef]] = defaultdict(list)
    for ref in previous_remaining:
        if ref.canonical_key:
            previous_by_canonical[ref.canonical_key].append(ref)
    for ref in current_remaining:
        if ref.canonical_key:
            current_by_canonical[ref.canonical_key].append(ref)

    for canonical_key in sorted(previous_by_canonical.keys() & current_by_canonical.keys()):
        previous_group = list(previous_by_canonical[canonical_key])
        current_group = list(current_by_canonical[canonical_key])
        canonical_pairs = _pair_canonical_group(previous_group, current_group)
        for previous_ref, current_ref in canonical_pairs:
            pairs.append((previous_ref, current_ref, IdentityBasis.VERIFIED_CANONICAL))
            _remove_ref(previous_remaining, previous_ref)
            _remove_ref(current_remaining, current_ref)

    previous_by_raw: dict[str, list[_ItemRef]] = defaultdict(list)
    current_by_raw: dict[str, list[_ItemRef]] = defaultdict(list)
    for ref in previous_remaining:
        previous_by_raw[ref.raw_key].append(ref)
    for ref in current_remaining:
        current_by_raw[ref.raw_key].append(ref)

    for raw_key in sorted(previous_by_raw.keys() & current_by_raw.keys()):
        previous_group = [ref for ref in previous_by_raw[raw_key] if ref in previous_remaining]
        current_group = [ref for ref in current_by_raw[raw_key] if ref in current_remaining]
        if not previous_group or not current_group:
            continue
        # Canonical identity remains authoritative when both sides are verified.
        if all(not ref.pending for ref in previous_group + current_group):
            continue
        if len(previous_group) != 1 or len(current_group) != 1:
            raise AmbiguousPrescriptionDiffError("raw identity is not uniquely pairable")
        previous_ref = previous_group[0]
        current_ref = current_group[0]
        pairs.append((previous_ref, current_ref, IdentityBasis.RAW_EXACT))
        _remove_ref(previous_remaining, previous_ref)
        _remove_ref(current_remaining, current_ref)

    return pairs, previous_remaining, current_remaining


def _evidence(
    previous: _ItemRef | None, current: _ItemRef | None
) -> dict[str, str | None]:
    return {
        "previous_encounter_id": str(previous.encounter.id) if previous else None,
        "current_encounter_id": str(current.encounter.id) if current else None,
        "previous_document_id": (
            str(previous.encounter.primary_document_id) if previous else None
        ),
        "current_document_id": str(current.encounter.primary_document_id) if current else None,
        "previous_prescription_item_id": str(previous.item.id) if previous else None,
        "current_prescription_item_id": str(current.item.id) if current else None,
    }


def _base_item(
    previous: _ItemRef | None,
    current: _ItemRef | None,
    *,
    identity_basis: IdentityBasis,
) -> dict[str, Any]:
    previous_item = previous.item if previous else None
    current_item = current.item if current else None
    canonical_identity = None
    if current_item and current_item.canonical_drug_name:
        canonical_identity = current_item.canonical_drug_name
    elif previous_item and previous_item.canonical_drug_name:
        canonical_identity = previous_item.canonical_drug_name
    return {
        "primary_change": "",
        "change_types": [],
        "identity_basis": identity_basis.value,
        "canonical_identity": canonical_identity,
        "previous_raw_name": previous_item.raw_drug_name if previous_item else None,
        "current_raw_name": current_item.raw_drug_name if current_item else None,
        "previous_processing": previous_item.processing_method if previous_item else None,
        "current_processing": current_item.processing_method if current_item else None,
        "processing_changed": False,
        "previous_dose_g": _decimal(previous.comparable_dose) if previous else None,
        "current_dose_g": _decimal(current.comparable_dose) if current else None,
        "signed_change_g": None,
        "absolute_change_g": None,
        "dose_change": DoseChange.NOT_APPLICABLE.value,
        "previous_dose_scope": previous_item.dose_scope.value if previous_item else None,
        "current_dose_scope": current_item.dose_scope.value if current_item else None,
        "previous_normalization_status": (
            previous_item.normalization_status if previous_item else None
        ),
        "current_normalization_status": (
            current_item.normalization_status if current_item else None
        ),
        "evidence": _evidence(previous, current),
    }


def _matched_item(
    previous: _ItemRef, current: _ItemRef, identity_basis: IdentityBasis
) -> dict[str, Any]:
    item = _base_item(previous, current, identity_basis=identity_basis)
    changes: list[PrescriptionChange] = []

    processing_changed = previous.processing_key != current.processing_key
    item["processing_changed"] = processing_changed
    if processing_changed:
        changes.append(PrescriptionChange.PROCESSING_CHANGED)

    previous_dose = previous.comparable_dose
    current_dose = current.comparable_dose
    if previous_dose is None or current_dose is None:
        item["dose_change"] = DoseChange.UNKNOWN.value
    else:
        delta = current_dose - previous_dose
        item["signed_change_g"] = _decimal(delta)
        item["absolute_change_g"] = _decimal(abs(delta))
        if delta > 0:
            item["dose_change"] = DoseChange.INCREASED.value
            changes.insert(0, PrescriptionChange.DOSE_INCREASED)
        elif delta < 0:
            item["dose_change"] = DoseChange.DECREASED.value
            changes.insert(0, PrescriptionChange.DOSE_DECREASED)
        else:
            item["dose_change"] = DoseChange.UNCHANGED.value

    if not changes:
        changes.append(PrescriptionChange.UNCHANGED)
    if previous.pending or current.pending:
        changes.append(PrescriptionChange.NORMALIZATION_PENDING)

    item["change_types"] = [change.value for change in changes]
    item["primary_change"] = changes[0].value
    return item


def _unmatched_item(ref: _ItemRef, *, added: bool) -> dict[str, Any]:
    item = _base_item(
        None if added else ref,
        ref if added else None,
        identity_basis=(
            IdentityBasis.RAW_EXACT if ref.pending else IdentityBasis.VERIFIED_CANONICAL
        ),
    )
    primary = PrescriptionChange.ADDED if added else PrescriptionChange.REMOVED
    changes = [primary]
    if ref.pending:
        changes.append(PrescriptionChange.NORMALIZATION_PENDING)
    item["primary_change"] = primary.value
    item["change_types"] = [change.value for change in changes]
    return item


def _sort_item(item: dict[str, Any]) -> tuple[int, str, str]:
    priority = {
        PrescriptionChange.ADDED.value: 0,
        PrescriptionChange.REMOVED.value: 1,
        PrescriptionChange.DOSE_INCREASED.value: 2,
        PrescriptionChange.DOSE_DECREASED.value: 3,
        PrescriptionChange.PROCESSING_CHANGED.value: 4,
        PrescriptionChange.UNCHANGED.value: 5,
    }
    label = item["canonical_identity"] or item["current_raw_name"] or item["previous_raw_name"]
    evidence = item["evidence"]
    stable_item_id = (
        evidence["current_prescription_item_id"]
        or evidence["previous_prescription_item_id"]
        or ""
    )
    return priority[item["primary_change"]], _identity_key(label), stable_item_id


def compare_prescriptions(previous: Encounter, current: Encounter) -> dict[str, Any]:
    """Return a deterministic, read-only diff between two same-patient encounters."""

    if previous.patient_id != current.patient_id:
        raise ValueError("encounters must belong to the same patient")

    pairs, removed, added = _match_items(_flatten(previous), _flatten(current))
    items = [
        _matched_item(previous_ref, current_ref, identity_basis)
        for previous_ref, current_ref, identity_basis in pairs
    ]
    items.extend(_unmatched_item(ref, added=False) for ref in removed)
    items.extend(_unmatched_item(ref, added=True) for ref in added)
    items.sort(key=_sort_item)

    summary = {
        "total": len(items),
        "added": sum(PrescriptionChange.ADDED.value in item["change_types"] for item in items),
        "removed": sum(
            PrescriptionChange.REMOVED.value in item["change_types"] for item in items
        ),
        "dose_increased": sum(
            PrescriptionChange.DOSE_INCREASED.value in item["change_types"] for item in items
        ),
        "dose_decreased": sum(
            PrescriptionChange.DOSE_DECREASED.value in item["change_types"] for item in items
        ),
        "processing_changed": sum(
            PrescriptionChange.PROCESSING_CHANGED.value in item["change_types"] for item in items
        ),
        "unchanged": sum(
            PrescriptionChange.UNCHANGED.value in item["change_types"] for item in items
        ),
        "normalization_pending": sum(
            PrescriptionChange.NORMALIZATION_PENDING.value in item["change_types"]
            for item in items
        ),
        "unknown_dose": sum(item["dose_change"] == DoseChange.UNKNOWN.value for item in items),
    }
    return {
        "algorithm_version": PRESCRIPTION_DIFF_ALGORITHM_VERSION,
        "dose_basis": DoseScope.PER_DECOCTION.value,
        "previous_encounter": {
            "id": str(previous.id),
            "encounter_at": previous.encounter_at,
            "document_id": str(previous.primary_document_id),
        },
        "current_encounter": {
            "id": str(current.id),
            "encounter_at": current.encounter_at,
            "document_id": str(current.primary_document_id),
        },
        "summary": summary,
        "items": items,
    }
