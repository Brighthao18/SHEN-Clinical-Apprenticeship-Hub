from __future__ import annotations

import hashlib
import io
import logging
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.ocr.base import OCRProviderError, OCRResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MinerURuntimeConfig:
    """Resolved MinerU connection parameters (never logged)."""

    base_url: str
    api_token: str
    model_version: str


def resolve_mineru_runtime_config(
    settings: Any,
) -> MinerURuntimeConfig | None:
    """Token resolution priority: environment bootstrap, then the UI slot.

    The environment token (``MINERU_API_TOKEN``) is the bootstrap/ops path
    and wins. Otherwise the runtime ``provider_settings`` slot
    ``mineru_ocr`` maintained by the settings UI supplies base_url,
    model_version, and the token (encrypted at rest). The backend-only
    ``ALLOW_IDENTIFIABLE_MINERU_UPLOAD`` switch is checked separately by
    the caller and gates BOTH paths. A database lookup failure never
    breaks ingestion; the caller fails closed on ``None``.
    """

    if settings.mineru_api_token:
        return MinerURuntimeConfig(
            base_url=settings.mineru_base_url.rstrip("/"),
            api_token=settings.mineru_api_token,
            model_version=settings.mineru_model_version,
        )
    try:
        from app.db.session import SessionLocal
        from app.services.ai.provider_config import (
            MINERU_PROVIDER_SLOT,
            resolve_provider_params,
        )

        with SessionLocal() as session:
            params = resolve_provider_params(session, MINERU_PROVIDER_SLOT)
        if params is not None:
            if params.get("confirmed_identifiable_upload") is not True:
                # The slot's stored privacy decision is missing; the
                # original-file boundary stays closed (fail-closed).
                logger.warning(
                    "MinerU settings slot lacks the explicit identifiable-upload "
                    "confirmation; cloud OCR stays disabled"
                )
                return None
            return MinerURuntimeConfig(
                base_url=str(params["base_url"]),
                api_token=str(params["api_key"]),
                model_version=str(params["model"]),
            )
    except Exception:  # noqa: BLE001 - config lookup must never break ingestion
        logger.warning("MinerU settings slot lookup failed", exc_info=True)
    return None


class MinerUOCRProvider:
    """MinerU precision API signed-upload adapter.

    The original source is transmitted to MinerU. Signed URLs are retained only inside the
    protected raw provider result and are never logged.
    """

    name = "mineru"

    def extract(
        self,
        path: Path,
        *,
        explicit_text: str | None = None,
        on_submitted: Callable[[str], None] | None = None,
    ) -> OCRResult:
        settings = get_settings()
        if not settings.allow_identifiable_mineru_upload:
            raise OCRProviderError(
                "MINERU_IDENTIFIABLE_UPLOAD_NOT_ALLOWED",
                "Identifiable MinerU upload is disabled by backend policy",
            )
        config = resolve_mineru_runtime_config(settings)
        if config is None:
            raise OCRProviderError(
                "MINERU_TOKEN_MISSING",
                "MinerU token is not configured (environment or settings UI slot)",
            )

        base_url = config.base_url
        headers = {"Authorization": f"Bearer {config.api_token}"}
        data_id = hashlib.sha256(path.read_bytes()).hexdigest()
        request_payload = {
            "files": [{"name": path.name, "data_id": data_id}],
            "model_version": config.model_version,
            "language": settings.mineru_language,
            "enable_formula": False,
            "enable_table": True,
        }

        try:
            with httpx.Client(timeout=30.0, follow_redirects=True) as client:
                submit = client.post(
                    f"{base_url}/api/v4/file-urls/batch",
                    headers={**headers, "Content-Type": "application/json"},
                    json=request_payload,
                )
                self._raise_http(submit, "MINERU_SUBMIT_FAILED")
                submit_json = submit.json()
                self._raise_api(submit_json, "MINERU_SUBMIT_REJECTED")
                batch_id = str(submit_json["data"]["batch_id"])
                if on_submitted is not None:
                    on_submitted(batch_id)
                upload_urls = submit_json["data"].get("file_urls") or []
                if len(upload_urls) != 1:
                    raise OCRProviderError(
                        "MINERU_UPLOAD_URL_INVALID", "MinerU did not return one upload URL"
                    )

                with path.open("rb") as source:
                    upload = client.put(upload_urls[0], content=source)
                self._raise_http(upload, "MINERU_UPLOAD_FAILED")

                final_json = self._poll(client, base_url, headers, batch_id)
                result = final_json["data"]["extract_result"][0]
                zip_url = result.get("full_zip_url")
                if not zip_url:
                    raise OCRProviderError(
                        "MINERU_RESULT_URL_MISSING", "MinerU completed without a result archive"
                    )
                archive_response = client.get(zip_url)
                self._raise_http(archive_response, "MINERU_RESULT_DOWNLOAD_FAILED")
                text, members = self._read_full_markdown(archive_response.content)
        except httpx.TimeoutException as exc:
            raise OCRProviderError(
                "MINERU_TIMEOUT", "MinerU request timed out", retryable=True
            ) from exc
        except httpx.NetworkError as exc:
            raise OCRProviderError(
                "MINERU_NETWORK_ERROR", "MinerU network request failed", retryable=True
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise OCRProviderError(
                "MINERU_RESPONSE_INVALID", "MinerU returned an unexpected response"
            ) from exc

        raw_result: dict[str, Any] = {
            "submit_response": submit_json,
            "final_response": final_json,
            "archive_sha256": hashlib.sha256(archive_response.content).hexdigest(),
            "archive_members": members,
            "full_markdown": text,
        }
        return OCRResult(
            provider="mineru",
            provider_version=f"precision-v4/{config.model_version}",
            text=text,
            raw_result=raw_result,
            external_task_id=batch_id,
        )

    def _poll(
        self,
        client: httpx.Client,
        base_url: str,
        headers: dict[str, str],
        batch_id: str,
    ) -> dict[str, Any]:
        settings = get_settings()
        deadline = time.monotonic() + settings.mineru_timeout_seconds
        while time.monotonic() < deadline:
            response = client.get(
                f"{base_url}/api/v4/extract-results/batch/{batch_id}", headers=headers
            )
            self._raise_http(response, "MINERU_POLL_FAILED")
            payload = response.json()
            self._raise_api(payload, "MINERU_POLL_REJECTED")
            results = payload.get("data", {}).get("extract_result") or []
            if results:
                state = results[0].get("state")
                if state == "done":
                    return payload
                if state == "failed":
                    raise OCRProviderError(
                        "MINERU_EXTRACTION_FAILED",
                        "MinerU extraction failed; inspect protected provider metadata",
                    )
            time.sleep(settings.mineru_poll_interval_seconds)
        raise OCRProviderError(
            "MINERU_POLL_TIMEOUT",
            "MinerU extraction exceeded the configured deadline",
            retryable=True,
        )

    @staticmethod
    def _raise_http(response: httpx.Response, code: str) -> None:
        if response.is_success:
            return
        retryable = response.status_code == 429 or response.status_code >= 500
        raise OCRProviderError(code, f"MinerU HTTP {response.status_code}", retryable=retryable)

    @staticmethod
    def _raise_api(payload: dict[str, Any], code: str) -> None:
        if payload.get("code") == 0:
            return
        raise OCRProviderError(code, "MinerU API rejected the request")

    @staticmethod
    def _read_full_markdown(data: bytes) -> tuple[str, list[str]]:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                safe_members: list[str] = []
                markdown_name: str | None = None
                for info in archive.infolist():
                    path = PurePosixPath(info.filename)
                    if path.is_absolute() or ".." in path.parts:
                        raise OCRProviderError(
                            "MINERU_ARCHIVE_UNSAFE", "MinerU result archive has an unsafe path"
                        )
                    safe_members.append(info.filename)
                    if path.name == "full.md":
                        markdown_name = info.filename
                if markdown_name is None:
                    raise OCRProviderError(
                        "MINERU_MARKDOWN_MISSING", "MinerU result archive has no full.md"
                    )
                return archive.read(markdown_name).decode("utf-8"), safe_members
        except zipfile.BadZipFile as exc:
            raise OCRProviderError(
                "MINERU_ARCHIVE_INVALID", "MinerU result is not a valid ZIP archive"
            ) from exc
