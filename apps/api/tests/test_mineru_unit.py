"""MinerU adapter unit tests: runtime-config resolution, the full signed
upload → poll → archive flow, and every fail-closed error branch.

MinerU cloud OCR is an explicit privacy boundary (it receives original
files): the backend switch and token resolution both stay fail-closed.
No network access — the HTTP client is a scripted fake.
"""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.db.session import SessionLocal
from app.services.ai.provider_config import (
    MINERU_ADAPTER,
    MINERU_PROVIDER_SLOT,
    save_provider_setting,
)
from app.services.ocr.base import OCRProviderError
from app.services.ocr.mineru import MinerUOCRProvider, resolve_mineru_runtime_config


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "allow_identifiable_mineru_upload": True,
        "mineru_api_token": "test-token",
        "mineru_base_url": "https://mineru.example/",
        "mineru_model_version": "vlm",
        "mineru_language": "ch",
        "mineru_timeout_seconds": 5,
        "mineru_poll_interval_seconds": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeResponse:
    def __init__(
        self, payload: dict[str, Any] | None = None, content: bytes = b"", status_code: int = 200
    ) -> None:
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.is_success = status_code < 400

    def json(self) -> dict[str, Any]:
        return self._payload  # type: ignore[return-value]


def _archive_bytes(*members: tuple[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, text in members:
            archive.writestr(name, text)
    return buffer.getvalue()


class _ScriptedClient:
    """Answers submit/put/get from a scripted queue; records URLs."""

    responses: dict[str, Any] = {}
    calls: list[tuple[str, str]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        assert "timeout" in kwargs

    def __enter__(self) -> _ScriptedClient:
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        type(self).calls.append(("post", url))
        return type(self).responses["post"]

    def put(self, url: str, **kwargs: Any) -> _FakeResponse:
        type(self).calls.append(("put", url))
        return type(self).responses["put"]

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        type(self).calls.append(("get", url))
        handler = type(self).responses.get("get")
        if callable(handler):
            return handler(url)
        return type(self).responses["get"]


def _install(monkeypatch: pytest.MonkeyPatch, responses: dict[str, Any]) -> list[tuple[str, str]]:
    _ScriptedClient.responses = responses
    _ScriptedClient.calls = []
    monkeypatch.setattr("app.services.ocr.mineru.httpx.Client", _ScriptedClient)
    monkeypatch.setattr("app.services.ocr.mineru.get_settings", lambda: _settings())
    return _ScriptedClient.calls


# ---------------------------------------------------------------------------
# Runtime config resolution (env bootstrap wins; UI slot needs confirmation)
# ---------------------------------------------------------------------------


def test_runtime_config_env_token_wins_and_strips_trailing_slash() -> None:
    config = resolve_mineru_runtime_config(_settings())
    assert config is not None
    assert config.base_url == "https://mineru.example"
    assert config.api_token == "test-token"
    assert config.model_version == "vlm"


def test_runtime_config_uses_confirmed_settings_slot(db_session) -> None:
    with SessionLocal() as session:
        save_provider_setting(
            session,
            slot=MINERU_PROVIDER_SLOT,
            adapter=MINERU_ADAPTER,
            base_url="https://slot.example",
            model="vlm",
            api_key="slot-token",
            confirmed_identifiable_upload=True,
        )
        session.commit()
        config = resolve_mineru_runtime_config(_settings(mineru_api_token=None))
    assert config is not None
    assert config.base_url == "https://slot.example"
    assert config.api_token == "slot-token"


def test_runtime_config_slot_without_confirmation_stays_closed() -> None:
    # No environment token and no configured slot: fail closed.
    assert resolve_mineru_runtime_config(_settings(mineru_api_token=None)) is None


def test_runtime_config_survives_database_lookup_failure(monkeypatch) -> None:
    class _ExplodingSession:
        def __enter__(self):
            raise RuntimeError("database unavailable")

        def __exit__(self, *args: Any) -> None:
            return None

    monkeypatch.setattr("app.db.session.SessionLocal", _ExplodingSession)
    assert resolve_mineru_runtime_config(_settings(mineru_api_token=None)) is None


def test_extract_without_any_config_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("app.services.ocr.mineru.get_settings", lambda: _settings())
    monkeypatch.setattr(
        "app.services.ocr.mineru.resolve_mineru_runtime_config", lambda settings: None
    )
    source = tmp_path / "case.jpg"
    source.write_bytes(b"private-source")
    with pytest.raises(OCRProviderError) as excinfo:
        MinerUOCRProvider().extract(source)
    assert excinfo.value.code == "MINERU_TOKEN_MISSING"


# ---------------------------------------------------------------------------
# Signed upload → poll → archive flow
# ---------------------------------------------------------------------------


def _submit_ok(file_urls: list[str]) -> _FakeResponse:
    return _FakeResponse({"code": 0, "data": {"batch_id": "job-1", "file_urls": file_urls}})


def _done_poll() -> _FakeResponse:
    return _FakeResponse(
        {"code": 0, "data": {"extract_result": [{"state": "done", "full_zip_url": "zip-url"}]}}
    )


def test_extract_full_flow_reads_archive_and_records_members(monkeypatch, tmp_path) -> None:
    calls = _install(
        monkeypatch,
        {
            "post": _submit_ok(["upload-url"]),
            "put": _FakeResponse(),
            "get": _done_poll(),
            "download": _FakeResponse(content=_archive_bytes(("out/full.md", "# 正文"))),
        },
    )

    class _Client(_ScriptedClient):
        def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            type(self).calls.append(("get", url))
            if url == "zip-url":
                return type(self).responses["download"]
            return type(self).responses["get"]

    monkeypatch.setattr("app.services.ocr.mineru.httpx.Client", _Client)
    source = tmp_path / "case.jpg"
    source.write_bytes(b"private-source")

    result = MinerUOCRProvider().extract(source)

    assert result.external_task_id == "job-1"
    assert result.provider_version == "precision-v4/vlm"
    assert result.text == "# 正文"
    assert result.raw_result["archive_sha256"]
    assert result.raw_result["archive_members"] == ["out/full.md"]
    urls = [url for _, url in calls]
    assert urls.count("https://mineru.example/api/v4/file-urls/batch") == 1
    assert "upload-url" in urls
    assert "https://mineru.example/api/v4/extract-results/batch/job-1" in urls
    assert "zip-url" in urls


def test_extract_rejects_wrong_upload_url_count(monkeypatch, tmp_path) -> None:
    _install(monkeypatch, {"post": _submit_ok(["a", "b"])})
    source = tmp_path / "case.jpg"
    source.write_bytes(b"private-source")
    with pytest.raises(OCRProviderError) as excinfo:
        MinerUOCRProvider().extract(source)
    assert excinfo.value.code == "MINERU_UPLOAD_URL_INVALID"


def test_extract_http_429_is_retryable(monkeypatch, tmp_path) -> None:
    _install(monkeypatch, {"post": _FakeResponse(status_code=429)})
    source = tmp_path / "case.jpg"
    source.write_bytes(b"private-source")
    with pytest.raises(OCRProviderError) as excinfo:
        MinerUOCRProvider().extract(source)
    assert excinfo.value.code == "MINERU_SUBMIT_FAILED"
    assert excinfo.value.retryable is True


def test_extract_api_rejection_fails_closed(monkeypatch, tmp_path) -> None:
    _install(monkeypatch, {"post": _FakeResponse({"code": 1, "data": {}})})
    source = tmp_path / "case.jpg"
    source.write_bytes(b"private-source")
    with pytest.raises(OCRProviderError) as excinfo:
        MinerUOCRProvider().extract(source)
    assert excinfo.value.code == "MINERU_SUBMIT_REJECTED"


def test_extract_poll_failure_state_fails_closed(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        {
            "post": _submit_ok(["upload-url"]),
            "put": _FakeResponse(),
            "get": _FakeResponse(
                {"code": 0, "data": {"extract_result": [{"state": "failed"}]}}
            ),
        },
    )
    source = tmp_path / "case.jpg"
    source.write_bytes(b"private-source")
    with pytest.raises(OCRProviderError) as excinfo:
        MinerUOCRProvider().extract(source)
    assert excinfo.value.code == "MINERU_EXTRACTION_FAILED"


def test_extract_poll_deadline_times_out_retryable(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "app.services.ocr.mineru.httpx.Client", _ScriptedClient
    )
    monkeypatch.setattr(
        "app.services.ocr.mineru.get_settings",
        lambda: _settings(mineru_timeout_seconds=0),
    )
    _ScriptedClient.responses = {"post": _submit_ok(["upload-url"]), "put": _FakeResponse()}
    _ScriptedClient.calls = []
    source = tmp_path / "case.jpg"
    source.write_bytes(b"private-source")
    with pytest.raises(OCRProviderError) as excinfo:
        MinerUOCRProvider().extract(source)
    assert excinfo.value.code == "MINERU_POLL_TIMEOUT"
    assert excinfo.value.retryable is True


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (httpx.ConnectTimeout("timed out"), "MINERU_TIMEOUT"),
        (httpx.ConnectError("refused"), "MINERU_NETWORK_ERROR"),
    ],
)
def test_extract_transport_errors_are_retryable_and_redacted(
    monkeypatch, tmp_path, error: Exception, expected_code: str
) -> None:
    class _FailingClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FailingClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, *args: Any, **kwargs: Any) -> _FakeResponse:
            raise error

    monkeypatch.setattr("app.services.ocr.mineru.httpx.Client", _FailingClient)
    monkeypatch.setattr("app.services.ocr.mineru.get_settings", lambda: _settings())
    source = tmp_path / "case.jpg"
    source.write_bytes(b"private-source")
    with pytest.raises(OCRProviderError) as excinfo:
        MinerUOCRProvider().extract(source)
    assert excinfo.value.code == expected_code
    assert excinfo.value.retryable is True


def test_extract_unexpected_response_shape_fails_closed(monkeypatch, tmp_path) -> None:
    # done without full_zip_url → KeyError → MINERU_RESPONSE_INVALID.
    _install(
        monkeypatch,
        {
            "post": _submit_ok(["upload-url"]),
            "put": _FakeResponse(),
            "get": _FakeResponse({"code": 0, "data": {"extract_result": [{"state": "done"}]}}),
        },
    )
    source = tmp_path / "case.jpg"
    source.write_bytes(b"private-source")
    with pytest.raises(OCRProviderError) as excinfo:
        MinerUOCRProvider().extract(source)
    assert excinfo.value.code == "MINERU_RESULT_URL_MISSING"


# ---------------------------------------------------------------------------
# Archive reading
# ---------------------------------------------------------------------------


def test_archive_without_full_md_is_rejected(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        {
            "post": _submit_ok(["upload-url"]),
            "put": _FakeResponse(),
            "get": _done_poll(),
            "download": _FakeResponse(content=_archive_bytes(("out/other.md", "x"))),
        },
    )

    class _Client(_ScriptedClient):
        def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            type(self).calls.append(("get", url))
            if url == "zip-url":
                return type(self).responses["download"]
            return type(self).responses["get"]

    monkeypatch.setattr("app.services.ocr.mineru.httpx.Client", _Client)
    source = tmp_path / "case.jpg"
    source.write_bytes(b"private-source")
    with pytest.raises(OCRProviderError) as excinfo:
        MinerUOCRProvider().extract(source)
    assert excinfo.value.code == "MINERU_MARKDOWN_MISSING"


def test_archive_with_unsafe_member_path_is_rejected(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        {
            "post": _submit_ok(["upload-url"]),
            "put": _FakeResponse(),
            "get": _done_poll(),
            "download": _FakeResponse(
                content=_archive_bytes(("../escape.md", "x"), ("full.md", "y"))
            ),
        },
    )

    class _Client(_ScriptedClient):
        def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            type(self).calls.append(("get", url))
            if url == "zip-url":
                return type(self).responses["download"]
            return type(self).responses["get"]

    monkeypatch.setattr("app.services.ocr.mineru.httpx.Client", _Client)
    source = tmp_path / "case.jpg"
    source.write_bytes(b"private-source")
    with pytest.raises(OCRProviderError) as excinfo:
        MinerUOCRProvider().extract(source)
    assert excinfo.value.code == "MINERU_ARCHIVE_UNSAFE"


def test_archive_invalid_zip_is_rejected(monkeypatch, tmp_path) -> None:
    _install(
        monkeypatch,
        {
            "post": _submit_ok(["upload-url"]),
            "put": _FakeResponse(),
            "get": _done_poll(),
            "download": _FakeResponse(content=b"not-a-zip"),
        },
    )

    class _Client(_ScriptedClient):
        def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            type(self).calls.append(("get", url))
            if url == "zip-url":
                return type(self).responses["download"]
            return type(self).responses["get"]

    monkeypatch.setattr("app.services.ocr.mineru.httpx.Client", _Client)
    source = tmp_path / "case.jpg"
    source.write_bytes(b"private-source")
    with pytest.raises(OCRProviderError) as excinfo:
        MinerUOCRProvider().extract(source)
    assert excinfo.value.code == "MINERU_ARCHIVE_INVALID"
