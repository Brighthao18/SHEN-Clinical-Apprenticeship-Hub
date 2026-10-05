from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.services.ai.provider_config import (
    APPRENTICESHIP_WRITING_PROVIDER_SLOT,
    LONGITUDINAL_PROVIDER_SLOT,
    resolve_provider_params,
)


@dataclass(frozen=True)
class AIProviderResult:
    provider: str
    model: str
    output: str
    structured_output: dict[str, Any]


class AIProvider(Protocol):
    def run(
        self, *, prompt: str, context: dict[str, Any], schema: dict[str, Any]
    ) -> AIProviderResult: ...


class AIProviderError(RuntimeError):
    """Redacted provider failure safe to surface through the API."""


class LiveStructuredAIProvider:
    """OpenAI-compatible, low-temperature structured-output text adapter.

    This adapter receives only the already de-identified context produced by
    ``ClinicalContextBuilder``. Request bodies and provider responses are not
    logged. The task service remains responsible for strict schema and
    evidence-reference validation after this JSON transport boundary.
    """

    def __init__(self, *, base_url: str, model: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key

    def run(
        self, *, prompt: str, context: dict[str, Any], schema: dict[str, Any]
    ) -> AIProviderResult:
        settings = get_settings()
        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        context_text = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        payload = {
            "model": self._model,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{prompt}\n\n输出必须严格符合以下 JSON Schema；不要输出 Markdown、"
                        f"解释或 schema 外字段：\n{schema_text}"
                    ),
                },
                {"role": "user", "content": context_text},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            with httpx.Client(timeout=settings.ai_provider_timeout_seconds) as client:
                response = client.post(
                    f"{self._base_url}/chat/completions",
                    headers=headers,
                    json={**payload, "response_format": {"type": "json_object"}},
                )
                if response.status_code == 400:
                    response = client.post(
                        f"{self._base_url}/chat/completions", headers=headers, json=payload
                    )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AIProviderError(
                f"AI provider returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AIProviderError(
                f"AI provider transport failed: {type(exc).__name__}"
            ) from exc

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
            structured = json.loads(content)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AIProviderError("AI provider did not return a valid JSON object") from exc
        if not isinstance(structured, dict):
            raise AIProviderError("AI provider JSON root must be an object")
        return AIProviderResult(
            provider="live_openai_compatible",
            model=self._model,
            output=content,
            structured_output=structured,
        )


class MockAIProvider:
    def run(
        self, *, prompt: str, context: dict[str, Any], schema: dict[str, Any]
    ) -> AIProviderResult:
        return AIProviderResult(
            provider="mock",
            model="deterministic-test",
            output="Mock analysis: no clinical inference was generated.",
            structured_output={"status": "mock", "evidence_count": len(context.get("facts", []))},
        )


def get_longitudinal_analysis_provider(session: Session) -> AIProvider | None:
    """Resolve the dedicated Phase 8 text-provider slot, or fail closed."""

    params = resolve_provider_params(session, LONGITUDINAL_PROVIDER_SLOT)
    if params is None:
        return None
    if params["adapter"] != "live_openai_compatible":
        return None
    return LiveStructuredAIProvider(
        base_url=params["base_url"],
        model=params["model"],
        api_key=params["api_key"],
    )


def get_apprenticeship_writing_provider(session: Session) -> AIProvider | None:
    """Resolve the dedicated Phase 9 text-provider slot, or fail closed."""

    params = resolve_provider_params(session, APPRENTICESHIP_WRITING_PROVIDER_SLOT)
    if params is None:
        return None
    if params["adapter"] != "live_openai_compatible":
        return None
    return LiveStructuredAIProvider(
        base_url=params["base_url"],
        model=params["model"],
        api_key=params["api_key"],
    )


def context_hash(context: dict[str, Any]) -> str:
    encoded = json.dumps(context, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
