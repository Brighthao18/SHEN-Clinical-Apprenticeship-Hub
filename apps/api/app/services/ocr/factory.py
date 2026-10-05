from app.core.config import get_settings
from app.services.ocr.base import OCRProvider
from app.services.ocr.mineru import MinerUOCRProvider
from app.services.ocr.mock import MockOCRProvider


def get_ocr_provider() -> OCRProvider:
    provider = get_settings().ocr_provider
    if provider == "mock":
        return MockOCRProvider()
    if provider == "mineru":
        return MinerUOCRProvider()
    raise ValueError(f"Unsupported OCR provider: {provider}")
