"""Runtime AI provider configuration (Phase 6.1/6.2).

Slot-based settings storage for structured-output AI providers. The user
configures them through the authenticated settings API / UI; the API key is
encrypted at rest (EncryptedText, same field-level crypto as patient
identity) and is never returned by any API response or log.

Slots:
- ``clinical_extraction`` — the ClinicalAIExtractor provider (Phase 6.1);
- ``vision_verification`` — the image-capable provider used by the Vision
  Verification Fallback (Phase 6.2); its ``confirmed_identifiable_upload``
  is the explicit privacy decision that original case images may be sent to
  this provider.
- ``longitudinal_analysis`` — the de-identified text provider used only for
  Patient Longitudinal AI Analysis (Phase 8).
- ``apprenticeship_writing`` — the de-identified text provider used only
  for apprenticeship note drafting (Phase 9 跟师笔记 / 医案整理).
- ``mineru_ocr`` — the MinerU cloud OCR token (base_url/model_version/
  token). MinerU receives ORIGINAL files, so the backend-only
  ``ALLOW_IDENTIFIABLE_MINERU_UPLOAD`` switch stays mandatory even when
  this slot is configured; the slot never selects the OCR provider itself
  (that remains ``OCR_PROVIDER``).

Resolution priority (all slots):
1. Environment-configured live provider (bootstrap/ops path; extraction
   uses ``AI_EXTRACTION_PROVIDER`` + key, vision uses ``AI_VISION_PROVIDER_*``
   + the explicit upload switch, MinerU uses ``MINERU_API_TOKEN``);
2. Database slot (UI-configured) — the everyday path;
3. Mock/none (fail-closed).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.domain import ProviderSetting

logger = logging.getLogger(__name__)

PROVIDER_SLOT = "clinical_extraction"
VISION_PROVIDER_SLOT = "vision_verification"
LONGITUDINAL_PROVIDER_SLOT = "longitudinal_analysis"
APPRENTICESHIP_WRITING_PROVIDER_SLOT = "apprenticeship_writing"
MINERU_PROVIDER_SLOT = "mineru_ocr"
SUPPORTED_SLOTS = (
    PROVIDER_SLOT,
    VISION_PROVIDER_SLOT,
    LONGITUDINAL_PROVIDER_SLOT,
    APPRENTICESHIP_WRITING_PROVIDER_SLOT,
    MINERU_PROVIDER_SLOT,
)
SUPPORTED_ADAPTERS = ("live_openai_compatible", "mineru_cloud_api")
MINERU_ADAPTER = "mineru_cloud_api"
# ``model`` stores the MinerU model_version for the mineru_ocr slot.
MINERU_MODEL_VERSIONS = ("vlm", "pipeline")
# Plain HTTP is only acceptable to a provider on this machine (e.g. Ollama).
LOCAL_PROVIDER_HOSTS = frozenset({"localhost", "127.0.0.1"})


class ProviderConfigRejected(ValueError):
    """Raised when the submitted provider configuration is invalid."""


def _is_allowed_base_url(base_url: str) -> bool:
    """HTTPS to any host, or HTTP to an exact local host.

    A prefix check would accept ``http://localhost.example.com`` or
    ``http://localhost@example.com`` and send the key plus identifiable
    inputs in cleartext off the machine. Embedded credentials are refused
    on every scheme: base_url is shown in the UI and the audit trail.
    """

    if any(character.isspace() for character in base_url):
        return False
    try:
        parsed = urlsplit(base_url)
        parsed.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        return False
    if not parsed.hostname or "@" in parsed.netloc:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in LOCAL_PROVIDER_HOSTS


def get_provider_setting(
    session: Session, slot: str = PROVIDER_SLOT
) -> ProviderSetting | None:
    return session.scalar(select(ProviderSetting).where(ProviderSetting.slot == slot))


def save_provider_setting(
    session: Session,
    *,
    adapter: str,
    base_url: str,
    model: str,
    api_key: str | None,
    confirmed_identifiable_upload: bool,
    slot: str = PROVIDER_SLOT,
) -> ProviderSetting:
    """Create or update a slot.

    ``api_key`` may be empty/None when the slot already stores a key: the
    stored key is retained so the user can change base_url/model without
    re-entering the secret. An empty key on a never-configured slot is
    rejected (the secret is mandatory the first time).
    """
    if slot not in SUPPORTED_SLOTS:
        raise ProviderConfigRejected(f"Unsupported provider slot: {slot}")
    if adapter not in SUPPORTED_ADAPTERS:
        raise ProviderConfigRejected(f"Unsupported provider adapter: {adapter}")
    if slot == MINERU_PROVIDER_SLOT:
        # MinerU is an OCR/document parser, not an OpenAI-compatible chat
        # provider; ``model`` carries the MinerU model_version instead.
        if adapter != MINERU_ADAPTER:
            raise ProviderConfigRejected(
                f"Slot {MINERU_PROVIDER_SLOT} requires the {MINERU_ADAPTER} adapter"
            )
        if model.strip() not in MINERU_MODEL_VERSIONS:
            raise ProviderConfigRejected(
                "MinerU model_version must be one of: " + ", ".join(MINERU_MODEL_VERSIONS)
            )
    elif adapter == MINERU_ADAPTER:
        raise ProviderConfigRejected(
            f"Adapter {MINERU_ADAPTER} is only valid for slot {MINERU_PROVIDER_SLOT}"
        )
    if not _is_allowed_base_url(base_url.strip()):
        raise ProviderConfigRejected(
            "base_url must be HTTPS, or an explicit localhost endpoint for a local "
            "provider such as Ollama"
        )
    if not model.strip():
        raise ProviderConfigRejected("model is required")
    normalized_key = (api_key or "").strip()
    if not confirmed_identifiable_upload:
        raise ProviderConfigRejected(
            "The explicit data-transfer confirmation is required before a live provider "
            "can be enabled"
        )

    setting = get_provider_setting(session, slot)
    if normalized_key:
        if setting is None:
            setting = ProviderSetting(slot=slot, adapter=adapter)
            session.add(setting)
        # Assignment triggers EncryptedText encryption; the plaintext never
        # rests in the database.
        setting.api_key_encrypted = normalized_key
    elif setting is None:
        raise ProviderConfigRejected(
            "api_key is required: this slot has no stored key to retain"
        )
    setting.adapter = adapter
    setting.base_url = base_url.strip().rstrip("/")
    setting.model = model.strip()
    setting.confirmed_identifiable_upload = True
    setting.enabled = True
    session.flush()
    return setting


def clear_provider_setting(session: Session, slot: str = PROVIDER_SLOT) -> bool:
    setting = get_provider_setting(session, slot)
    if setting is None:
        return False
    session.delete(setting)
    session.flush()
    return True


def resolve_provider_params(session: Session, slot: str = PROVIDER_SLOT) -> dict[str, Any] | None:
    """Enabled UI-configured provider parameters, or ``None``."""

    setting = get_provider_setting(session, slot)
    if setting is None or not setting.enabled:
        return None
    if not _is_allowed_base_url(setting.base_url):
        # A row saved before the stricter check (or edited by hand) must not
        # reach a cleartext remote endpoint; behave as unconfigured.
        logger.warning(
            "Provider slot %s base_url fails the transport policy; slot stays inactive",
            slot,
            extra={"error_code": "PROVIDER_BASE_URL_REJECTED"},
        )
        return None
    return {
        "slot": setting.slot,
        "adapter": setting.adapter,
        "base_url": setting.base_url,
        "model": setting.model,
        "api_key": setting.api_key_encrypted,
        "confirmed_identifiable_upload": setting.confirmed_identifiable_upload,
    }


def resolve_extraction_provider_params(session: Session) -> dict[str, Any] | None:
    """Enabled UI-configured extraction provider parameters, or ``None``."""

    return resolve_provider_params(session, PROVIDER_SLOT)
