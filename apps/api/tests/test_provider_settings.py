"""Phase 6.1/6.2 runtime AI provider settings: API surface + factory resolution.

Covers the slots (``clinical_extraction``, ``vision_verification``,
``longitudinal_analysis``, ``mineru_ocr``): API shape, encrypted key
storage, per-slot audit, and the vision/MinerU factories' resolution
priority (environment bootstrap path, then the UI-configured slot, else
inactive/fail-closed).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.domain import AuditEvent, ProviderSetting
from app.services.ai.extractor import (
    MockClinicalExtractionProvider,
    get_clinical_extraction_provider,
)
from app.services.ai.gateway import (
    LiveStructuredAIProvider,
    get_longitudinal_analysis_provider,
)
from app.services.ai.live_provider import LiveOpenAICompatibleProvider
from app.services.ai.vision_verifier import (
    LiveVisionVerificationProvider,
    get_vision_verification_provider,
)

PAYLOAD = {
    "adapter": "live_openai_compatible",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "model": "glm-4.7",
    "api_key": "secret-test-key-1234567890",
    "confirmed_identifiable_upload": True,
}

VISION_PAYLOAD = {
    "adapter": "live_openai_compatible",
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "model": "glm-4.6v",
    "api_key": "secret-vision-key-1234567890",
    "confirmed_identifiable_upload": True,
    "slot": "vision_verification",
}

LONGITUDINAL_PAYLOAD = {
    "adapter": "live_openai_compatible",
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "api_key": "secret-longitudinal-key-1234567890",
    "confirmed_identifiable_upload": True,
    "slot": "longitudinal_analysis",
}

MINERU_PAYLOAD = {
    "adapter": "mineru_cloud_api",
    "base_url": "https://mineru.net",
    "model": "vlm",
    "api_key": "secret-mineru-token-1234567890",
    "confirmed_identifiable_upload": True,
    "slot": "mineru_ocr",
}


def _put(client, auth_headers, payload: dict) -> dict:
    return client.put("/api/v1/settings/ai-provider", headers=auth_headers, json=payload)


def test_ai_provider_requires_auth(client) -> None:
    assert client.get("/api/v1/settings/ai-provider").status_code == 401
    assert client.put("/api/v1/settings/ai-provider", json=PAYLOAD).status_code == 401
    assert client.delete("/api/v1/settings/ai-provider").status_code == 401


def test_unconfigured_state(client, auth_headers) -> None:
    response = client.get("/api/v1/settings/ai-provider", headers=auth_headers)
    assert response.status_code == 200
    slots = response.json()["slots"]
    assert slots["clinical_extraction"]["configured"] is False
    assert slots["clinical_extraction"]["adapter"] is None
    assert slots["vision_verification"]["configured"] is False
    assert slots["longitudinal_analysis"]["configured"] is False
    assert slots["mineru_ocr"]["configured"] is False


def test_put_rejects_missing_confirmation(client, auth_headers) -> None:
    response = _put(client, auth_headers, {**PAYLOAD, "confirmed_identifiable_upload": False})
    assert response.status_code == 422


def test_put_rejects_unknown_adapter(client, auth_headers) -> None:
    response = _put(client, auth_headers, {**PAYLOAD, "adapter": "magic_parser"})
    assert response.status_code == 422


def test_put_rejects_unknown_slot(client, auth_headers) -> None:
    response = _put(client, auth_headers, {**PAYLOAD, "slot": "magic_slot"})
    assert response.status_code == 422


def test_saved_key_is_encrypted_and_never_returned(client, auth_headers) -> None:
    response = _put(client, auth_headers, PAYLOAD)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True
    assert body["slot"] == "clinical_extraction"
    assert body["api_key_masked"].startswith("••••")
    assert "secret-test-key" not in response.text

    with SessionLocal() as session:
        setting = session.scalar(select(ProviderSetting))
        assert setting is not None
        # Raw ciphertext (bypassing the decrypting TypeDecorator) never
        # contains the plaintext key; the attribute decrypts for the factory.
        raw_cipher = session.execute(
            text("SELECT api_key_encrypted FROM provider_settings")
        ).scalar_one()
        assert "secret-test-key" not in str(raw_cipher)
        assert str(setting.api_key_encrypted) == PAYLOAD["api_key"]
        audit_count = session.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.action == "ai_provider.configured")
        )
        assert audit_count == 1

    fetched = client.get("/api/v1/settings/ai-provider", headers=auth_headers).json()
    assert fetched["slots"]["clinical_extraction"]["model"] == "glm-4.7"
    assert "secret-test-key" not in str(fetched)


def test_factory_resolves_runtime_config_then_mock(client, auth_headers) -> None:
    # Default: mock fixture provider.
    assert isinstance(get_clinical_extraction_provider(), MockClinicalExtractionProvider)

    assert _put(client, auth_headers, PAYLOAD).status_code == 200
    provider = get_clinical_extraction_provider()
    assert isinstance(provider, LiveOpenAICompatibleProvider)

    # Clearing the slot restores the fail-closed mock provider.
    deleted = client.delete("/api/v1/settings/ai-provider", headers=auth_headers)
    assert deleted.status_code == 200
    assert deleted.json()["cleared"] is True
    assert isinstance(get_clinical_extraction_provider(), MockClinicalExtractionProvider)


def test_vision_slot_is_independent_of_extraction_slot(client, auth_headers) -> None:
    assert _put(client, auth_headers, PAYLOAD).status_code == 200
    response = _put(client, auth_headers, VISION_PAYLOAD)
    assert response.status_code == 200, response.text
    assert response.json()["slot"] == "vision_verification"

    fetched = client.get("/api/v1/settings/ai-provider", headers=auth_headers).json()
    assert fetched["slots"]["clinical_extraction"]["model"] == "glm-4.7"
    assert fetched["slots"]["vision_verification"]["model"] == "glm-4.6v"
    assert "secret-vision-key" not in str(fetched)

    # Deleting the extraction slot leaves the vision slot untouched.
    deleted = client.delete("/api/v1/settings/ai-provider", headers=auth_headers)
    assert deleted.json()["slot"] == "clinical_extraction"
    fetched = client.get("/api/v1/settings/ai-provider", headers=auth_headers).json()
    assert fetched["slots"]["clinical_extraction"]["configured"] is False
    assert fetched["slots"]["vision_verification"]["configured"] is True

    deleted = client.delete(
        "/api/v1/settings/ai-provider?slot=vision_verification", headers=auth_headers
    )
    assert deleted.json()["cleared"] is True
    with SessionLocal() as session:
        assert session.scalar(select(func.count(ProviderSetting.id))) == 0


def test_longitudinal_slot_is_independent_and_resolves_gateway(
    client, auth_headers
) -> None:
    with SessionLocal() as session:
        assert get_longitudinal_analysis_provider(session) is None
    response = _put(client, auth_headers, LONGITUDINAL_PAYLOAD)
    assert response.status_code == 200, response.text
    assert response.json()["slot"] == "longitudinal_analysis"
    with SessionLocal() as session:
        provider = get_longitudinal_analysis_provider(session)
        assert isinstance(provider, LiveStructuredAIProvider)
    fetched = client.get("/api/v1/settings/ai-provider", headers=auth_headers).json()
    assert fetched["slots"]["longitudinal_analysis"]["model"] == "deepseek-chat"
    assert "secret-longitudinal-key" not in str(fetched)


def test_vision_factory_requires_capability_flag(client, auth_headers) -> None:
    assert _put(client, auth_headers, VISION_PAYLOAD).status_code == 200
    settings = get_settings()
    original = settings.ai_vision_verification_enabled
    settings.ai_vision_verification_enabled = False
    try:
        assert get_vision_verification_provider() is None
    finally:
        settings.ai_vision_verification_enabled = original


def test_vision_factory_resolves_ui_slot(client, auth_headers, monkeypatch) -> None:
    assert _put(client, auth_headers, VISION_PAYLOAD).status_code == 200
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_vision_verification_enabled", True, raising=False)
    # No environment bootstrap configured: the UI slot is the only source.
    monkeypatch.setattr(settings, "ai_vision_provider_api_key", None, raising=False)
    monkeypatch.setattr(
        settings, "allow_identifiable_ai_vision_upload", False, raising=False
    )

    provider = get_vision_verification_provider()
    assert isinstance(provider, LiveVisionVerificationProvider)
    name, model = provider.describe()
    assert name == "live_openai_compatible"
    assert model == "glm-4.6v"

    # Clearing the slot deactivates the fallback again.
    deleted = client.delete(
        "/api/v1/settings/ai-provider?slot=vision_verification", headers=auth_headers
    )
    assert deleted.status_code == 200
    assert get_vision_verification_provider() is None


def test_vision_factory_env_bootstrap_path(client, auth_headers, monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_vision_verification_enabled", True, raising=False)
    monkeypatch.setattr(settings, "ai_vision_provider_api_key", "env-bootstrap-key", raising=False)
    monkeypatch.setattr(settings, "ai_vision_provider_model", "glm-4.6v", raising=False)
    monkeypatch.setattr(settings, "allow_identifiable_ai_vision_upload", True, raising=False)

    provider = get_vision_verification_provider()
    assert isinstance(provider, LiveVisionVerificationProvider)
    _, model = provider.describe()
    assert model == "glm-4.6v"

    # Without the backend-only upload switch the env path stays closed.
    monkeypatch.setattr(settings, "allow_identifiable_ai_vision_upload", False, raising=False)
    assert get_vision_verification_provider() is None


def test_mineru_slot_rejects_chat_adapter_and_unknown_model_version(
    client, auth_headers
) -> None:
    # MinerU is an OCR/document parser slot, not an OpenAI-compatible chat
    # provider: its own adapter and model_version vocabulary are enforced.
    response = _put(
        client, auth_headers, {**MINERU_PAYLOAD, "adapter": "live_openai_compatible"}
    )
    assert response.status_code == 422

    response = _put(client, auth_headers, {**MINERU_PAYLOAD, "model": "glm-4.7"})
    assert response.status_code == 422

    # The MinerU adapter is not valid for chat slots either.
    response = _put(client, auth_headers, {**PAYLOAD, "adapter": "mineru_cloud_api"})
    assert response.status_code == 422


def test_mineru_slot_is_independent_and_encrypts_token(client, auth_headers) -> None:
    assert _put(client, auth_headers, PAYLOAD).status_code == 200
    response = _put(client, auth_headers, MINERU_PAYLOAD)
    assert response.status_code == 200, response.text
    assert response.json()["slot"] == "mineru_ocr"
    assert response.json()["api_key_masked"].startswith("••••")
    assert "secret-mineru-token" not in response.text

    fetched = client.get("/api/v1/settings/ai-provider", headers=auth_headers).json()
    assert fetched["slots"]["mineru_ocr"]["configured"] is True
    assert fetched["slots"]["mineru_ocr"]["model"] == "vlm"
    assert fetched["slots"]["clinical_extraction"]["configured"] is True
    assert "secret-mineru-token" not in str(fetched)

    with SessionLocal() as session:
        raw_cipher = session.execute(
            text("SELECT api_key_encrypted FROM provider_settings WHERE slot = 'mineru_ocr'")
        ).scalar_one()
        assert "secret-mineru-token" not in str(raw_cipher)

    # Deleting the extraction slot leaves the MinerU slot untouched.
    deleted = client.delete("/api/v1/settings/ai-provider", headers=auth_headers)
    assert deleted.json()["slot"] == "clinical_extraction"
    fetched = client.get("/api/v1/settings/ai-provider", headers=auth_headers).json()
    assert fetched["slots"]["mineru_ocr"]["configured"] is True

    deleted = client.delete(
        "/api/v1/settings/ai-provider?slot=mineru_ocr", headers=auth_headers
    )
    assert deleted.json()["cleared"] is True
    fetched = client.get("/api/v1/settings/ai-provider", headers=auth_headers).json()
    assert fetched["slots"]["mineru_ocr"]["configured"] is False


def test_mineru_runtime_config_resolves_env_first_then_ui_slot(
    client, auth_headers, monkeypatch
) -> None:
    from app.services.ocr.mineru import resolve_mineru_runtime_config

    settings = get_settings()

    # Bootstrap/ops path: an environment token wins over the UI slot.
    monkeypatch.setattr(settings, "mineru_api_token", "env-bootstrap-token", raising=False)
    env_config = resolve_mineru_runtime_config(settings)
    assert env_config is not None
    assert env_config.api_token == "env-bootstrap-token"

    # No environment token: the UI-configured slot is the only source, and
    # it fails closed while unconfigured.
    monkeypatch.setattr(settings, "mineru_api_token", None, raising=False)
    assert resolve_mineru_runtime_config(settings) is None

    assert _put(client, auth_headers, MINERU_PAYLOAD).status_code == 200
    config = resolve_mineru_runtime_config(settings)
    assert config is not None
    assert config.api_token == MINERU_PAYLOAD["api_key"]
    assert config.base_url == "https://mineru.net"
    assert config.model_version == "vlm"

    # Clearing the slot deactivates the UI path again.
    deleted = client.delete(
        "/api/v1/settings/ai-provider?slot=mineru_ocr", headers=auth_headers
    )
    assert deleted.json()["cleared"] is True
    assert resolve_mineru_runtime_config(settings) is None


def test_put_without_api_key_retains_stored_key(client, auth_headers) -> None:
    # First-time configuration still requires the secret.
    response = _put(client, auth_headers, {**PAYLOAD, "api_key": ""})
    assert response.status_code == 422

    assert _put(client, auth_headers, PAYLOAD).status_code == 200
    # Updating base_url/model without re-entering the key keeps the stored
    # secret (the plaintext never leaves the server).
    response = _put(
        client,
        auth_headers,
        {**PAYLOAD, "api_key": "", "base_url": "https://api.deepseek.com"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["base_url"] == "https://api.deepseek.com"
    assert response.json()["api_key_masked"].startswith("••••")

    with SessionLocal() as session:
        setting = session.scalar(select(ProviderSetting))
        assert setting is not None
        assert str(setting.api_key_encrypted) == PAYLOAD["api_key"]

    # The same retention works for the MinerU slot, including a model_version
    # switch, and the runtime resolver keeps using the retained token.
    from app.services.ocr.mineru import resolve_mineru_runtime_config

    settings = get_settings()
    assert _put(client, auth_headers, MINERU_PAYLOAD).status_code == 200
    response = _put(
        client,
        auth_headers,
        {**MINERU_PAYLOAD, "api_key": "", "model": "pipeline"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["model"] == "pipeline"
    config = resolve_mineru_runtime_config(settings)
    assert config is not None
    assert config.api_token == MINERU_PAYLOAD["api_key"]
    assert config.model_version == "pipeline"

    for slot in ("clinical_extraction", "mineru_ocr"):
        client.delete(f"/api/v1/settings/ai-provider?slot={slot}", headers=auth_headers)


def test_patch_toggles_enabled_state_without_deleting_credentials(
    client, auth_headers
) -> None:
    from sqlalchemy import func

    from app.services.ai.provider_config import resolve_provider_params

    assert _put(client, auth_headers, PAYLOAD).status_code == 200

    disabled = client.patch(
        "/api/v1/settings/ai-provider",
        headers=auth_headers,
        json={"slot": "clinical_extraction", "enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    assert disabled.json()["configured"] is True
    with SessionLocal() as session:
        assert resolve_provider_params(session, "clinical_extraction") is None
    assert isinstance(get_clinical_extraction_provider(), MockClinicalExtractionProvider)

    enabled = client.patch(
        "/api/v1/settings/ai-provider",
        headers={**auth_headers},
        json={"slot": "clinical_extraction", "enabled": True},
    )
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    with SessionLocal() as session:
        assert resolve_provider_params(session, "clinical_extraction") is not None
    assert isinstance(get_clinical_extraction_provider(), LiveOpenAICompatibleProvider)

    with SessionLocal() as session:
        toggles = session.scalar(
            select(func.count(AuditEvent.id)).where(AuditEvent.action == "ai_provider.toggled")
        )
        assert toggles == 2

    # Toggling an unconfigured slot is a 404, not a silent create.
    missing = client.patch(
        "/api/v1/settings/ai-provider",
        headers=auth_headers,
        json={"slot": "vision_verification", "enabled": False},
    )
    assert missing.status_code == 404

    client.delete("/api/v1/settings/ai-provider", headers=auth_headers)


@pytest.mark.parametrize(
    "base_url",
    [
        "https://open.bigmodel.cn/api/paas/v4",
        "HTTPS://api.deepseek.com",
        "http://127.0.0.1:11434/v1",
        "http://localhost:11434/v1",
        "http://LOCALHOST:11434/v1",
    ],
)
def test_put_accepts_https_or_exact_local_http(client, auth_headers, base_url: str) -> None:
    response = _put(
        client, auth_headers, {**PAYLOAD, "slot": "longitudinal_analysis", "base_url": base_url}
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "base_url",
    [
        # Cleartext to a remote host that merely starts like a local one.
        "http://localhost.attacker.example/v1",
        "http://localhost@attacker.example/v1",
        "http://127.0.0.1.attacker.example/v1",
        "http://127.0.0.1@attacker.example/v1",
        "http://localhostattacker.example",
        "http://api.deepseek.com",
        "http://[::1]:11434/v1",
        # Credentials would be echoed to the UI and the audit trail.
        "https://user:secret@api.deepseek.com",
        # No usable host, port or scheme.
        "https://",
        "https://:443/v1",
        "http://localhost:99999/v1",
        "https://api.deep seek.com",
        "ftp://localhost/v1",
        "localhost:11434/v1",
    ],
)
def test_put_rejects_cleartext_remote_or_malformed_base_url(
    client, auth_headers, base_url: str
) -> None:
    response = _put(
        client, auth_headers, {**PAYLOAD, "slot": "longitudinal_analysis", "base_url": base_url}
    )
    assert response.status_code == 422, response.text
    assert "base_url must be HTTPS" in response.json()["detail"]
    with SessionLocal() as session:
        assert session.scalar(select(func.count(ProviderSetting.id))) == 0


def test_stored_cleartext_remote_base_url_resolves_inactive(client, auth_headers) -> None:
    from app.services.ai.provider_config import resolve_provider_params

    saved = _put(client, auth_headers, {**PAYLOAD, "slot": "longitudinal_analysis"})
    assert saved.status_code == 200
    with SessionLocal() as session:
        # A row written before the stricter check, or edited by hand.
        session.execute(
            text("UPDATE provider_settings SET base_url = 'http://localhost.attacker.example'")
        )
        session.commit()
        assert resolve_provider_params(session, "longitudinal_analysis") is None
        assert get_longitudinal_analysis_provider(session) is None
    # The slot stays visible so the user can correct it in settings.
    fetched = client.get("/api/v1/settings/ai-provider", headers=auth_headers).json()
    assert fetched["slots"]["longitudinal_analysis"]["configured"] is True
