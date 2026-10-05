from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class OCRProviderError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class OCRResult:
    provider: str
    provider_version: str
    text: str
    raw_result: dict[str, Any]
    external_task_id: str | None = None


class OCRProvider(Protocol):
    name: str

    def extract(
        self,
        path: Path,
        *,
        explicit_text: str | None = None,
        on_submitted: Callable[[str], None] | None = None,
    ) -> OCRResult: ...
