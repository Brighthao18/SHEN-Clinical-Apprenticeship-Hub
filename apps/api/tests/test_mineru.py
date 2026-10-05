from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest

from app.services.ocr.base import OCRProviderError
from app.services.ocr.mineru import MinerUOCRProvider


def test_mineru_gate_blocks_before_source_file_is_read(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "app.services.ocr.mineru.get_settings",
        lambda: SimpleNamespace(
            allow_identifiable_mineru_upload=False,
            mineru_api_token="configured-but-disabled",
        ),
    )
    missing_path = tmp_path / "must-not-be-read.jpg"

    with pytest.raises(OCRProviderError) as caught:
        MinerUOCRProvider().extract(missing_path)

    assert caught.value.code == "MINERU_IDENTIFIABLE_UPLOAD_NOT_ALLOWED"


def test_mineru_missing_token_fails_closed_before_source_file_is_read(
    monkeypatch, tmp_path
) -> None:
    # No environment token and no UI slot: extraction fails closed without
    # touching the source file (the runtime config lookup returns None).
    monkeypatch.setattr(
        "app.services.ocr.mineru.get_settings",
        lambda: SimpleNamespace(
            allow_identifiable_mineru_upload=True,
            mineru_api_token=None,
        ),
    )
    monkeypatch.setattr(
        "app.services.ocr.mineru.resolve_mineru_runtime_config", lambda _settings: None
    )
    missing_path = tmp_path / "must-not-be-read.jpg"

    with pytest.raises(OCRProviderError) as caught:
        MinerUOCRProvider().extract(missing_path)

    assert caught.value.code == "MINERU_TOKEN_MISSING"


def test_mineru_records_provider_job_identifier_immediately_after_submission(
    monkeypatch, tmp_path
) -> None:
    settings = SimpleNamespace(
        allow_identifiable_mineru_upload=True,
        mineru_api_token="test-token",
        mineru_base_url="https://mineru.example",
        mineru_model_version="vlm",
        mineru_language="ch",
        mineru_timeout_seconds=5,
        mineru_poll_interval_seconds=0,
    )
    monkeypatch.setattr("app.services.ocr.mineru.get_settings", lambda: settings)

    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w") as archive:
        archive.writestr("result/full.md", "校准文本")

    class FakeResponse:
        def __init__(self, payload=None, content=b"") -> None:
            self._payload = payload
            self.content = content
            self.status_code = 200
            self.is_success = True

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            pass

        def post(self, *args, **kwargs):
            return FakeResponse(
                {"code": 0, "data": {"batch_id": "job-123", "file_urls": ["upload"]}}
            )

        def put(self, *args, **kwargs):
            return FakeResponse()

        def get(self, url, *args, **kwargs):
            if url == "result-url":
                return FakeResponse(content=archive_buffer.getvalue())
            return FakeResponse(
                {
                    "code": 0,
                    "data": {"extract_result": [{"state": "done", "full_zip_url": "result-url"}]},
                }
            )

    monkeypatch.setattr("app.services.ocr.mineru.httpx.Client", FakeClient)
    source = tmp_path / "case.jpg"
    source.write_bytes(b"private-source")
    submitted_ids: list[str] = []

    result = MinerUOCRProvider().extract(source, on_submitted=submitted_ids.append)

    assert submitted_ids == ["job-123"]
    assert result.external_task_id == "job-123"
    assert result.text == "校准文本"
