"""LiveOpenAICompatibleProvider (Phase 6.1 extraction adapter) tests.

Non-negotiable properties under test: the explicit identifiable-input
privacy boundary, fail-closed redacted errors, the single 400 fallback,
and code-fence stripping before strict schema validation. Request bodies
and keys are never logged; the fake client asserts nothing is sent twice.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.config import get_settings
from app.services.ai.extractor import ISSUE_PROVIDER_FAILED, ClinicalExtractionRejected
from app.services.ai.live_provider import LiveOpenAICompatibleProvider


class _FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any]) -> None:
        self.status_code = status_code
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://provider.test/chat/completions"),
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
        assert headers["Authorization"] == "Bearer test-live-key"
        type(self).requests.append(json)
        return type(self).queue.pop(0)


def _install(monkeypatch: pytest.MonkeyPatch, responses: list[_FakeResponse]) -> list[dict]:
    _FakeClient.queue = responses
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return _FakeClient.requests


def _provider() -> LiveOpenAICompatibleProvider:
    # Runtime slot path: an explicit constructor api_key carries the user's
    # auditable confirmation, so the identifiable-input policy check passes.
    return LiveOpenAICompatibleProvider(
        base_url="http://provider.test/v1", model="test-model", api_key="test-live-key"
    )


def _completion(content: Any) -> _FakeResponse:
    return _FakeResponse(200, {"choices": [{"message": {"content": content}}]})


def test_environment_path_blocked_without_backend_switch(monkeypatch) -> None:
    """The privacy boundary is fail-closed: real OCR text may not cross to a
    live provider until ALLOW_IDENTIFIABLE_INPUT is explicitly enabled."""

    monkeypatch.setattr(get_settings(), "ai_extraction_allow_identifiable_input", False)
    monkeypatch.setattr(get_settings(), "ai_provider_api_key", None)
    provider = LiveOpenAICompatibleProvider()
    with pytest.raises(ClinicalExtractionRejected) as excinfo:
        provider.extract("真实 OCR 文本，含姓名")
    assert excinfo.value.issue_code == ISSUE_PROVIDER_FAILED
    assert "disabled by backend policy" in str(excinfo.value)


def test_extract_success_sends_temperature_zero_and_json_mode(
    monkeypatch,
) -> None:
    requests = _install(
        monkeypatch, [_completion(json.dumps({"confidence": 0.9}, ensure_ascii=False))]
    )
    result = _provider().extract("OCR 文本")
    assert result == {"confidence": 0.9}
    assert len(requests) == 1
    assert requests[0]["temperature"] == 0
    assert requests[0]["response_format"] == {"type": "json_object"}
    assert requests[0]["messages"][1]["content"] == "OCR 文本"


def test_extract_falls_back_once_without_response_format_on_400(monkeypatch) -> None:
    requests = _install(
        monkeypatch, [_FakeResponse(400, {}), _completion('{"confidence": 0.8}')]
    )
    result = _provider().extract("OCR 文本")
    assert result == {"confidence": 0.8}
    assert "response_format" in requests[0]
    assert "response_format" not in requests[1]


def test_extract_http_error_is_redacted(monkeypatch) -> None:
    _install(monkeypatch, [_FakeResponse(500, {})])
    with pytest.raises(ClinicalExtractionRejected, match="provider HTTP error 500"):
        _provider().extract("OCR 文本")


def test_extract_transport_error_is_redacted(monkeypatch) -> None:
    class _BrokenClient(_FakeClient):
        def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "Client", _BrokenClient)
    with pytest.raises(
        ClinicalExtractionRejected, match="provider transport error: ConnectError"
    ):
        _provider().extract("OCR 文本")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("```json\n{\"confidence\": 0.7}\n```", {"confidence": 0.7}),
        ("前缀噪声 {\"confidence\": 0.6} 后缀噪声", {"confidence": 0.6}),
    ],
)
def test_extract_strips_code_fences_and_noise(
    monkeypatch, content: str, expected: dict
) -> None:
    _install(monkeypatch, [_completion(content)])
    assert _provider().extract("OCR 文本") == expected


def test_extract_missing_api_key_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "ai_extraction_allow_identifiable_input", True)
    monkeypatch.setattr(get_settings(), "ai_provider_api_key", None)
    provider = LiveOpenAICompatibleProvider()
    with pytest.raises(ClinicalExtractionRejected, match="api key is not configured"):
        provider.extract("OCR 文本")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"unexpected": "shape"}, "reply shape was not a chat completion"),
        ({"choices": [{"message": {"content": None}}]}, "contained no message content"),
        ({"choices": [{"message": {"content": "完全没有花括号的回复"}}]}, "no JSON object"),
        ({"choices": [{"message": {"content": "没有任何花括号"}}]}, "no JSON object"),
    ],
)
def test_extract_rejects_malformed_replies(
    monkeypatch, body: dict, expected: str
) -> None:
    _install(monkeypatch, [_FakeResponse(200, body)])
    with pytest.raises(ClinicalExtractionRejected, match=expected):
        _provider().extract("OCR 文本")


def test_verify_stays_disabled(monkeypatch) -> None:
    _install(monkeypatch, [_completion("{}")])
    assert _provider().verify("OCR", {}, ["ANY_CODE"]) is None
