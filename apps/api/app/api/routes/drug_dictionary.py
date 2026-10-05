from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.security import CurrentUser
from app.db.session import get_db
from app.models.domain import DrugDictionary
from app.services.audit import append_audit_event
from app.services.drug_candidates import (
    STATUS_DISABLED,
    STATUS_PENDING,
    STATUS_VERIFIED,
    DictionaryRejected,
    disable_entry,
    get_entry,
    list_entries,
    merge_entries,
    restore_entry,
    verify_entry,
)
from app.services.drugs import SpecificationExistsError, create_manual_verified_specification

router = APIRouter(prefix="/drug-dictionary", tags=["drug-dictionary"])
DbSession = Annotated[Session, Depends(get_db)]


class VerifyPayload(BaseModel):
    canonical_name: str = Field(min_length=1, max_length=160)
    processing_method: str | None = Field(default=None, max_length=120)


class PatchPayload(BaseModel):
    canonical_name: str | None = Field(default=None, min_length=1, max_length=160)
    processing_method: str | None = Field(default=None, max_length=120)
    aliases: list[str] | None = None
    hospital_display_names: list[str] | None = None


class MergePayload(BaseModel):
    target_id: uuid.UUID


class BatchVerifyPayload(BaseModel):
    ids: list[uuid.UUID] = Field(min_length=1, max_length=200)


class SpecificationPayload(BaseModel):
    """User-entered hospital reference specification (manual_verified).

    The dose value must come from the user's own trusted source (pharmacy
    label / prescription printout); AI and heuristics never write specs.
    """

    drug_dictionary_id: uuid.UUID
    hospital: str = Field(min_length=1, max_length=160)
    package_dose_g: Decimal = Field(gt=0, max_digits=10, decimal_places=3)
    original_package_unit: str = Field(min_length=1, max_length=40)
    processing_method: str | None = Field(default=None, max_length=120)
    effective_from: date | None = None
    effective_to: date | None = None
    source_note: str | None = Field(default=None, max_length=500)


@router.get("")
def list_drug_dictionary(
    user: CurrentUser,
    db: DbSession,
    status: Annotated[str | None, Query(max_length=20)] = None,
    q: Annotated[str | None, Query(max_length=80)] = None,
) -> list[dict[str, Any]]:
    if status and status not in {STATUS_PENDING, STATUS_VERIFIED, STATUS_DISABLED}:
        raise HTTPException(status_code=422, detail="Unknown status filter")
    return list_entries(db, status=status, q=q)


@router.post("/{entry_id}/verify")
def verify_drug_entry(
    entry_id: uuid.UUID, payload: VerifyPayload, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    entry = get_entry(db, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Dictionary entry not found")
    try:
        backfilled = verify_entry(
            db,
            entry,
            canonical_name=payload.canonical_name,
            processing_method=payload.processing_method,
        )
    except DictionaryRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return {
        "verified": True,
        "canonical_name": entry.canonical_name,
        "backfilled_items": backfilled,
    }


@router.post("/batch-verify")
def batch_verify(payload: BatchVerifyPayload, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    """One-pass bootstrap confirmation: accept each entry's AI suggestion."""
    verified = 0
    backfilled = 0
    errors: list[str] = []
    for entry_id in payload.ids:
        entry = get_entry(db, entry_id)
        if entry is None:
            errors.append(f"{entry_id}: not found")
            continue
        try:
            backfilled += verify_entry(
                db,
                entry,
                canonical_name=entry.canonical_name,
                processing_method=entry.processing_method,
            )
            verified += 1
        except DictionaryRejected as exc:
            errors.append(f"{entry_id}: {exc}")
    db.commit()
    return {"verified": verified, "backfilled_items": backfilled, "errors": errors}


@router.post("/{entry_id}/disable")
def disable_drug_entry(entry_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    entry = get_entry(db, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Dictionary entry not found")
    disable_entry(db, entry)
    db.commit()
    return {"disabled": True}


@router.post("/{entry_id}/restore")
def restore_drug_entry(entry_id: uuid.UUID, user: CurrentUser, db: DbSession) -> dict[str, Any]:
    entry = get_entry(db, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Dictionary entry not found")
    restore_entry(db, entry)
    db.commit()
    return {"restored": True}


@router.post("/{entry_id}/merge")
def merge_drug_entry(
    entry_id: uuid.UUID, payload: MergePayload, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    source = get_entry(db, entry_id)
    target = get_entry(db, payload.target_id)
    if source is None or target is None:
        raise HTTPException(status_code=404, detail="Dictionary entry not found")
    try:
        merge_entries(db, source, target)
    except DictionaryRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    db.commit()
    return {"merged": True, "target_id": str(target.id)}


@router.patch("/{entry_id}")
def patch_drug_entry(
    entry_id: uuid.UUID, payload: PatchPayload, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    entry = get_entry(db, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Dictionary entry not found")
    if payload.canonical_name is not None:
        entry.canonical_name = payload.canonical_name.strip()
    if payload.processing_method is not None:
        entry.processing_method = payload.processing_method.strip() or None
    if payload.aliases is not None:
        entry.aliases = sorted({a.strip() for a in payload.aliases if a.strip()})
    if payload.hospital_display_names is not None:
        entry.hospital_display_names = sorted(
            {a.strip() for a in payload.hospital_display_names if a.strip()}
        )
    append_audit_event(
        db,
        action="drug_dictionary.edited",
        entity_type="drug_dictionary",
        entity_id=entry.id,
        actor_type="user",
        after_redacted={"canonical_name": entry.canonical_name},
    )
    db.commit()
    return {"updated": True}


@router.post("/specifications", status_code=201)
def create_hospital_specification(
    payload: SpecificationPayload, user: CurrentUser, db: DbSession
) -> dict[str, Any]:
    """Store a user-entered manual_verified hospital drug specification.

    Only VERIFIED dictionary entries accept specifications: PENDING
    candidates never resolve canonical identity at ingestion, and DISABLED
    entries are known-bad, so a dose reference on them would be unusable.
    """
    entry = db.get(DrugDictionary, payload.drug_dictionary_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Dictionary entry not found")
    if entry.status != STATUS_VERIFIED:
        raise HTTPException(
            status_code=422,
            detail="Specifications require a VERIFIED dictionary entry",
        )
    try:
        specification = create_manual_verified_specification(
            db,
            hospital=payload.hospital,
            dictionary_id=entry.id,
            package_dose_g=payload.package_dose_g,
            original_package_unit=payload.original_package_unit,
            processing_method=payload.processing_method,
            effective_from=payload.effective_from,
            effective_to=payload.effective_to,
            source_note=payload.source_note,
        )
    except SpecificationExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    append_audit_event(
        db,
        action="drug_specification.created",
        entity_type="hospital_drug_specification",
        entity_id=specification.id,
        actor_type="user",
        after_redacted={
            "hospital": specification.hospital,
            "drug_dictionary_id": str(entry.id),
            "package_dose_g": str(specification.package_dose_g),
            "normalized_package_unit": specification.normalized_package_unit,
            "processing_method": specification.processing_method,
            "effective_from": (
                specification.effective_from.isoformat()
                if specification.effective_from
                else None
            ),
            "version": specification.version,
            "source_type": specification.source_type,
        },
    )
    db.commit()
    return {
        "id": str(specification.id),
        "hospital": specification.hospital,
        "drug_dictionary_id": str(entry.id),
        "canonical_name": entry.canonical_name,
        "package_dose_g": str(specification.package_dose_g),
        "normalized_package_unit": specification.normalized_package_unit,
        "processing_method": specification.processing_method,
        "effective_from": (
            specification.effective_from.isoformat()
            if specification.effective_from
            else None
        ),
        "effective_to": (
            specification.effective_to.isoformat() if specification.effective_to else None
        ),
        "source_type": specification.source_type,
        "version": specification.version,
    }
