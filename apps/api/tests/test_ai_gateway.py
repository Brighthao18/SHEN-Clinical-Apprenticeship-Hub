"""Gateway-level tests: LiveStructuredAIProvider transport behavior and the
fail-closed dedicated-slot factories (Phase 8 longitudinal, Phase 9 writing).

The provider receives only de-identified context; request/response bodies are
never logged. These tests verify the JSON transport contract, the 400
fallback that retries without ``response_format``, and that every failure
surfaces as the redacted AIProviderError.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.db.session import SessionLocal
from app.services.ai.gateway import (
    AIProviderError,
    LiveStructuredAIProvider,
    get_apprenticeship_writing_provider,
    get_longitudinal_analysis_provider,
)
from app.services.ai.provider_config import (
    APPRENTICESHIP_WRITING_PROVIDER_SLOT,
    LONGITUDINAL_PROVIDER_SLOT,
    save_provider_setting,
)


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
    """Records each request payload; replies from a scripted queue."""

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
        assert headers["Authorization"].startswith("Bearer ")
        type(self).requests.append(json)
        if not type(self).queue:
            raise AssertionError("no scripted response")
        return type(self).queue.pop(0)


def _provider() -> LiveStructuredAIProvider:
    return LiveStructuredAIProvider(
        base_url="http://provider.test/v1", model="test-model", api_key="test-key"
    )


def _completion(content: str) -> _FakeResponse:
    return _FakeResponse(
        200, {"choices": [{"message": {"content": content}}]}
    )


def _install(monkeypatch: pytest.MonkeyPatch, responses: list[_FakeResponse]) -> list[dict]:
    _FakeClient.queue = responses
    _FakeClient.requests = []
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    return _FakeClient.requests


def test_run_success_returns_parsed_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install(
        monkeypatch,
        [_completion(json.dumps({"status": "ok", "score": 1}, ensure_ascii=False))],
    )
    result = _provider().run(
        prompt="系统提示", context={"facts": []}, schema={"type": "object"}
    )
    assert result.provider == "live_openai_compatible"
    assert result.model == "test-model"
    assert result.structured_output == {"status": "ok", "score": 1}
    # The strict JSON schema is embedded in the system message; the
    # de-identified context is the user message; json_object mode is set.
    assert "type" in requests[0]["messages"][0]["content"]
    assert requests[0]["response_format"] == {"type": "json_object"}


def test_run_falls_back_once_without_response_format_on_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = _install(monkeypatch, [_FakeResponse(400, {}), _completion("{}")])
    result = _provider().run(prompt="p", context={}, schema={})
    assert result.structured_output == {}
    assert len(requests) == 2
    assert "response_format" in requests[0]
    assert "response_format" not in requests[1]


def test_run_http_error_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, [_FakeResponse(500, {})])
    with pytest.raises(AIProviderError, match="AI provider returned HTTP 500"):
        _provider().run(prompt="p", context={}, schema={})


def test_run_transport_error_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    class _BrokenClient(_FakeClient):
        def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "Client", _BrokenClient)
    with pytest.raises(AIProviderError, match="transport failed: ConnectError"):
        _provider().run(prompt="p", context={}, schema={})


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("not-json", "did not return a valid JSON object"),
        ("[1, 2]", "JSON root must be an object"),
        ("", "did not return a valid JSON object"),
    ],
)
def test_run_rejects_non_object_payloads(
    monkeypatch: pytest.MonkeyPatch, content: str, expected: str
) -> None:
    _install(monkeypatch, [_completion(content)])
    with pytest.raises(AIProviderError, match=expected):
        _provider().run(prompt="p", context={}, schema={})


def test_dedicated_slots_fail_closed_when_unconfigured() -> None:
    with SessionLocal() as session:
        assert get_longitudinal_analysis_provider(session) is None
        assert get_apprenticeship_writing_provider(session) is None


def test_dedicated_slots_fail_closed_for_wrong_adapter() -> None:
    with SessionLocal() as session:
        setting = save_provider_setting(
            session,
            slot=LONGITUDINAL_PROVIDER_SLOT,
            adapter="live_openai_compatible",
            base_url="http://localhost:11434/v1",
            model="m",
            api_key="k",
            confirmed_identifiable_upload=True,
        )
        # Defense in depth: a tampered/manually-edited row with a non-text
        # adapter must resolve to None, never to a live provider.
        setting.adapter = "mineru_cloud_api"
        session.commit()
        assert get_longitudinal_analysis_provider(session) is None


def test_dedicated_slots_resolve_live_provider_when_configured() -> None:
    with SessionLocal() as session:
        for slot in (LONGITUDINAL_PROVIDER_SLOT, APPRENTICESHIP_WRITING_PROVIDER_SLOT):
            save_provider_setting(
                session,
                slot=slot,
                adapter="live_openai_compatible",
                base_url="http://localhost:11434/v1",
                model="test-model",
                api_key="secret-key",
                confirmed_identifiable_upload=True,
            )
        session.commit()
        longitudinal = get_longitudinal_analysis_provider(session)
        writing = get_apprenticeship_writing_provider(session)
        assert isinstance(longitudinal, LiveStructuredAIProvider)
        assert isinstance(writing, LiveStructuredAIProvider)
