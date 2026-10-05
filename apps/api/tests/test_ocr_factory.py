"""OCR provider factory tests: fail-closed on unknown provider names."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.services.ocr.factory import get_ocr_provider
from app.services.ocr.mineru import MinerUOCRProvider
from app.services.ocr.mock import MockOCRProvider


def test_factory_returns_mock_by_default() -> None:
    assert isinstance(get_ocr_provider(), MockOCRProvider)


def test_factory_returns_mineru_provider(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "ocr_provider", "mineru")
    assert isinstance(get_ocr_provider(), MinerUOCRProvider)


def test_factory_rejects_unknown_provider(monkeypatch) -> None:
    monkeypatch.setattr(get_settings(), "ocr_provider", "unknown-vendor")
    with pytest.raises(ValueError, match="Unsupported OCR provider"):
        get_ocr_provider()
