"""Vision Verification unit tests for the HTTP adapter and strict schema gates.

The apply-side semantics (constrained fields, unclear stays missing, masked
identity refusal) are covered by test_vision_verification.py; this module
covers the provider transport contract, the privacy gate, the strict
response schema, and the fail-closed provider factory.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.services.ai.provider_config import VISION_PROVIDER_SLOT, save_provider_setting
from app.services.ai.vision_verifier import (
    SOURCE_AI_VISUAL_VERIFICATION,
    LiveVisionVerificationProvider,
    VisionFieldRequest,
    VisionVerificationProviderError,
    VisionVerificationRejected,
    apply_vision_verifications,
    get_vision_verification_provider,
    parse_vision_verification_response,
)
from app.services.parsers.types import (
    ParsedDiagnosis,  # noqa: F401
    ParsedDocument,
    ParsedPrescriptionItem,
    ParsedSymptom,  # noqa: F401
)


def _parsed() -> ParsedDocument:
    from app.models.enums import Sex

    return ParsedDocument(
        template="AI",
        parser_version="test/1",
        hospital="暨南大学附属第一医院",
        real_name="张三",
        sex=Sex.FEMALE,
        age=40,
        encounter_date=None,
        medical_record_number=None,
        department=None,
        physician=None,
        chief_complaint=None,
        present_illness=None,
        past_history=None,
        allergy=None,
        vital_signs=None,
        advice=None,
        treatment_record=None,
        prescription_items=[
            ParsedPrescriptionItem(
                original_text="茯神（10g）2",
                is_basic_drug=True,
                raw_drug_name="茯神",
                processing_method=None,
                explicit_package_dose_g=None,
                package_count=None,
                original_package_unit=None,
                normalized_package_unit=None,
            )
        ],
    )


def _request() -> VisionFieldRequest:
    return VisionFieldRequest(
        field_path="prescription.items[0].original_package_unit",
        kind="package_unit",
        question="包装单位？",
        context="茯神（10g）2",
    )


def _verification(field_path: str, read_value: str | None, confidence: float) -> dict:
    return {
        "field_path": field_path,
        "read_value": read_value,
        "confidence": confidence,
        "evidence_quote": "（10g）2包",
    }


# ---------------------------------------------------------------------------
# Strict schema gates
# ---------------------------------------------------------------------------


def test_confidence_outside_range_is_rejected() -> None:
    with pytest.raises(VisionVerificationRejected, match="schema rejected"):
        parse_vision_verification_response(
            {"verifications": [_verification("x", "包", 1.5)]}
        )


def test_duplicate_field_answers_reject_the_whole_response() -> None:
    payload = {
        "verifications": [
            _verification("prescription.items[0].original_package_unit", "包", 0.99),
            _verification("prescription.items[0].original_package_unit", "袋", 0.99),
        ]
    }
    parsed = _parsed()
    response = parse_vision_verification_response(payload)
    with pytest.raises(VisionVerificationRejected, match="duplicate verification"):
        apply_vision_verifications(parsed, [_request()], response)


def test_schema_rejects_non_object_and_extra_fields() -> None:
    with pytest.raises(VisionVerificationRejected, match="schema rejected"):
        parse_vision_verification_response(["not", "an", "object"])
    with pytest.raises(VisionVerificationRejected, match="schema rejected"):
        parse_vision_verification_response({"verifications": [], "surprise": 1})


# ---------------------------------------------------------------------------
# LiveVisionVerificationProvider transport
# ---------------------------------------------------------------------------


def _provider() -> LiveVisionVerificationProvider:
    return LiveVisionVerificationProvider(
        base_url="http://vision.test/v1", model="vision-model", api_key="vision-key"
    )


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://vision.test/chat/completions"),
                response=httpx.Response(self.status_code),
            )

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeClient:
    queue: list[_FakeResponse] = []
    requests: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        assert "timeout" in kwargs

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]) -> _FakeResponse:
        assert url.endswith("/chat/completions")
        assert headers["Authorization"] == "Bearer vision-key"
        type(self).requests.append(json)
        return type(self).queue.pop(0)


def _install(monkeypatch: pytest.MonkeyPatch, responses: list[_FakeResponse]) -> list[dict]:
    _FakeClient.queue = responses
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return _FakeClient.requests


def test_environment_path_blocked_without_backend_switch(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "allow_identifiable_ai_vision_upload", False)
    provider = LiveVisionVerificationProvider()
    with pytest.raises(
        VisionVerificationProviderError, match="disabled by backend policy"
    ):
        provider.verify_fields(b"image", "image/jpeg", [_request()])


def test_missing_api_key_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "ai_vision_provider_api_key", None)
    provider = LiveVisionVerificationProvider()
    with pytest.raises(
        VisionVerificationProviderError, match="api key is not configured"
    ):
        provider.describe()


def test_verify_fields_sends_image_and_constrained_questions(monkeypatch) -> None:
    requests = _install(
        monkeypatch,
        [_FakeResponse(200, {"choices": [{"message": {"content": '{"verifications": []}'}}]})],
    )
    result = _provider().verify_fields(
        b"fake-image-bytes", "image/jpeg", [_request()]
    )
    assert result == {"verifications": []}
    sent = requests[0]
    assert sent["temperature"] == 0
    assert sent["response_format"] == {"type": "json_object"}
    content_parts = sent["messages"][0]["content"]
    text_part = content_parts[0]["text"]
    image_part = content_parts[1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"] == (
        "data:image/jpeg;base64,"
        + base64.b64encode(b"fake-image-bytes").decode("ascii")
    )
    assert "prescription.items[0].original_package_unit" in text_part
    assert "包装单位？" in text_part
    assert "茯神（10g）2" in text_part


def test_verify_fields_falls_back_once_without_response_format(monkeypatch) -> None:
    requests = _install(
        monkeypatch,
        [
            _FakeResponse(400, {}),
            _FakeResponse(200, {"choices": [{"message": {"content": "{}"}}]}),
        ],
    )
    result = _provider().verify_fields(b"img", "image/jpeg", [_request()])
    assert result == {}
    assert "response_format" in requests[0]
    assert "response_format" not in requests[1]


def test_verify_fields_http_error_is_redacted(monkeypatch) -> None:
    _install(monkeypatch, [_FakeResponse(500, {})])
    with pytest.raises(
        VisionVerificationProviderError, match="vision provider HTTP error 500"
    ):
        _provider().verify_fields(b"img", "image/jpeg", [_request()])


def test_verify_fields_transport_error_is_redacted(monkeypatch) -> None:
    class _BrokenClient(_FakeClient):
        def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "Client", _BrokenClient)
    with pytest.raises(
        VisionVerificationProviderError, match="transport error: ConnectError"
    ):
        _provider().verify_fields(b"img", "image/jpeg", [_request()])


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"unexpected": "shape"}, "reply shape was not a chat completion"),
        ({"choices": [{"message": {"content": None}}]}, "no message content"),
        ({"choices": [{"message": {"content": "没有花括号"}}]}, "no JSON object"),
        ({"choices": [{"message": {"content": "{broken}"}}]}, "not valid JSON"),
    ],
)
def test_verify_fields_rejects_malformed_replies(
    monkeypatch, body: dict, expected: str
) -> None:
    _install(monkeypatch, [_FakeResponse(200, body)])
    with pytest.raises(VisionVerificationProviderError, match=expected):
        _provider().verify_fields(b"img", "image/jpeg", [_request()])


# ---------------------------------------------------------------------------
# Provider factory (env bootstrap → UI slot → inactive)
# ---------------------------------------------------------------------------


def test_factory_inactive_when_capability_disabled(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "ai_vision_verification_enabled", False)
    assert get_vision_verification_provider() is None


def test_factory_env_bootstrap_requires_both_switches(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_vision_verification_enabled", True)
    monkeypatch.setattr(settings, "ai_vision_provider_api_key", "env-key")
    monkeypatch.setattr(settings, "allow_identifiable_ai_vision_upload", False)
    assert get_vision_verification_provider() is None
    monkeypatch.setattr(settings, "allow_identifiable_ai_vision_upload", True)
    provider = get_vision_verification_provider()
    assert isinstance(provider, LiveVisionVerificationProvider)


def test_factory_uses_confirmed_slot(monkeypatch, db_session) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_vision_verification_enabled", True)
    monkeypatch.setattr(settings, "ai_vision_provider_api_key", None)
    with SessionLocal() as session:
        save_provider_setting(
            session,
            slot=VISION_PROVIDER_SLOT,
            adapter="live_openai_compatible",
            base_url="http://localhost:11434/v1",
            model="vision-model",
            api_key="slot-key",
            confirmed_identifiable_upload=True,
        )
        session.commit()
        provider = get_vision_verification_provider()
    assert isinstance(provider, LiveVisionVerificationProvider)


def test_factory_survives_database_lookup_failure(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "ai_vision_verification_enabled", True)
    monkeypatch.setattr(settings, "ai_vision_provider_api_key", None)

    class _ExplodingSession:
        def __enter__(self):
            raise RuntimeError("database unavailable")

        def __exit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr("app.db.session.SessionLocal", _ExplodingSession)
    assert get_vision_verification_provider() is None


def test_provenance_source_marker_is_stable() -> None:
    parsed = _parsed()
    response = parse_vision_verification_response(
        {"verifications": [_verification(_request().field_path, "包", 0.99)]}
    )
    _, records = apply_vision_verifications(parsed, [_request()], response)
    assert records[0]["source"] == SOURCE_AI_VISUAL_VERIFICATION
    assert records[0]["read_value"] == "包"
    assert records[0]["applied"] is True
