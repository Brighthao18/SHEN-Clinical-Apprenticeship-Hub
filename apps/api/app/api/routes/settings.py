from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.security import CurrentUser
from app.db.session import get_db
from app.services.ai.provider_config import (
    LONGITUDINAL_PROVIDER_SLOT,
    PROVIDER_SLOT,
    SUPPORTED_SLOTS,
    VISION_PROVIDER_SLOT,
    ProviderConfigRejected,
    clear_provider_setting,
    get_provider_setting,
    save_provider_setting,
)
from app.services.audit import append_audit_event

router = APIRouter(prefix="/settings", tags=["settings"])
DbSession = Annotated[Session, Depends(get_db)]


class ProviderSettingUpdate(BaseModel):
    adapter: str
    base_url: str = Field(min_length=1, max_length=255)
    model: str = Field(min_length=1, max_length=80)
    # Optional on update: an empty value retains the already-stored key so
    # base_url/model can be changed without re-entering the secret. The
    # first-time configuration of a slot still requires it.
    api_key: str = Field(default="", max_length=400)
    # Explicit privacy approval: extraction inputs (OCR text) and vision
    # inputs (original case images) contain patient identifiers and will be
    # transmitted to the configured provider.
    confirmed_identifiable_upload: bool
    slot: str = PROVIDER_SLOT


class ProviderEnabledUpdate(BaseModel):
    slot: str = PROVIDER_SLOT
    enabled: bool


class ProviderSettingRead(BaseModel):
    slot: str
    adapter: str | None
    base_url: str | None
    model: str | None
    enabled: bool
    confirmed_identifiable_upload: bool
    configured: bool
    # Masked tail only; the full key never leaves the server.
    api_key_masked: str | None


def _serialize_setting(setting) -> ProviderSettingRead:
    key_tail = str(setting.api_key_encrypted)[-4:]
    return ProviderSettingRead(
        slot=setting.slot,
        adapter=setting.adapter,
        base_url=setting.base_url,
        model=setting.model,
        enabled=setting.enabled,
        confirmed_identifiable_upload=setting.confirmed_identifiable_upload,
        configured=True,
        api_key_masked=f"••••{key_tail}",
    )


def _unconfigured(slot: str) -> ProviderSettingRead:
    return ProviderSettingRead(
        slot=slot,
        adapter=None,
        base_url=None,
        model=None,
        enabled=False,
        confirmed_identifiable_upload=False,
        configured=False,
        api_key_masked=None,
    )


def _validated_slot(slot: str) -> str:
    if slot not in SUPPORTED_SLOTS:
        raise HTTPException(status_code=422, detail=f"Unsupported provider slot: {slot}")
    return slot


@router.get("/ai-provider")
def read_ai_provider(user: CurrentUser, db: DbSession) -> dict[str, Any]:
    slots: dict[str, ProviderSettingRead] = {}
    for slot in SUPPORTED_SLOTS:
        setting = get_provider_setting(db, slot)
        slots[slot] = _serialize_setting(setting) if setting else _unconfigured(slot)
    return {"slots": slots}


@router.put("/ai-provider")
def update_ai_provider(
    payload: ProviderSettingUpdate, user: CurrentUser, db: DbSession
) -> ProviderSettingRead:
    try:
        setting = save_provider_setting(
            db,
            slot=_validated_slot(payload.slot),
            adapter=payload.adapter,
            base_url=payload.base_url,
            model=payload.model,
            api_key=payload.api_key,
            confirmed_identifiable_upload=payload.confirmed_identifiable_upload,
        )
    except ProviderConfigRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    append_audit_event(
        db,
        action="ai_provider.configured",
        entity_type="provider_setting",
        entity_id=setting.id,
        actor_type="user",
        after_redacted={
            "slot": setting.slot,
            "adapter": setting.adapter,
            "base_url": setting.base_url,
            "model": setting.model,
        },
    )
    db.commit()
    return _serialize_setting(setting)


@router.patch("/ai-provider")
def toggle_ai_provider(
    payload: ProviderEnabledUpdate, user: CurrentUser, db: DbSession
) -> ProviderSettingRead:
    """Enable/disable a configured slot without deleting its credentials.

    A disabled slot is ignored by every runtime resolver (extraction,
    vision, longitudinal, writing, MinerU OCR), which falls back to the
    environment bootstrap path or fail-closed behavior.
    """
    slot = _validated_slot(payload.slot)
    setting = get_provider_setting(db, slot)
    if setting is None:
        raise HTTPException(status_code=404, detail=f"Slot {slot} is not configured")
    if setting.enabled != payload.enabled:
        setting.enabled = payload.enabled
        append_audit_event(
            db,
            action="ai_provider.toggled",
            entity_type="provider_setting",
            entity_id=setting.id,
            actor_type="user",
            after_redacted={"slot": slot, "enabled": payload.enabled},
        )
        db.commit()
    return _serialize_setting(setting)


@router.delete("/ai-provider", status_code=200)
def delete_ai_provider(
    user: CurrentUser,
    db: DbSession,
    slot: str = PROVIDER_SLOT,
) -> dict[str, Any]:
    _validated_slot(slot)
    removed = clear_provider_setting(db, slot)
    if removed:
        append_audit_event(
            db,
            action="ai_provider.cleared",
            entity_type="provider_setting",
            entity_id=uuid.uuid5(uuid.NAMESPACE_URL, f"provider_setting:{slot}"),
            actor_type="user",
            after_redacted={"slot": slot},
        )
        db.commit()
    return {"cleared": removed, "slot": slot}


# Re-exported for tests/importers that referenced the vision slot constant
# from the route module historically.
__all__ = ["LONGITUDINAL_PROVIDER_SLOT", "PROVIDER_SLOT", "VISION_PROVIDER_SLOT"]
