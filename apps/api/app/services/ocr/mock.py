from collections.abc import Callable
from pathlib import Path

from app.services.ocr.base import OCRProviderError, OCRResult


class MockOCRProvider:
    name = "mock"

    def extract(
        self,
        path: Path,
        *,
        explicit_text: str | None = None,
        on_submitted: Callable[[str], None] | None = None,
    ) -> OCRResult:
        if explicit_text is None or not explicit_text.strip():
            raise OCRProviderError(
                "MOCK_OCR_TEXT_REQUIRED",
                "Mock OCR requires explicit fixture text; it does not fabricate document content.",
            )
        text = explicit_text.replace("\r\n", "\n").replace("\r", "\n").strip()
        return OCRResult(
            provider="mock",
            provider_version="1.0",
            text=text,
            raw_result={
                "mode": "explicit_fixture",
                "source_filename": path.name,
                "character_count": len(text),
                "text": text,
            },
        )
