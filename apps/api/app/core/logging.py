from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

SENSITIVE_KEYS = {
    "name",
    "real_name",
    "phone",
    "id_card",
    "address",
    "medical_record_number",
    "mrn",
    "ocr_text",
    "prompt",
    "signed_url",
    "api_key",
    "authorization",
    "password",
    "token",
    "secret",
}


def _safe_value(key: str, value: Any) -> Any:
    if key.lower() in SENSITIVE_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {k: _safe_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(key, item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "correlation_id",
            "patient_id",
            "encounter_id",
            "document_id",
            "stage",
            "error_code",
        ):
            if hasattr(record, key):
                payload[key] = _safe_value(key, getattr(record, key))
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
