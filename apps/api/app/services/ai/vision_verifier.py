"""Vision Verification Fallback (Phase 6.2 — the final ingestion capability).

When the Validator blocks a document on OCR-source incompleteness, the
ORIGINAL case image may be sent ONCE to an image-capable provider for a
constrained per-field re-read. Non-negotiable properties:

- **Explicit privacy boundary**: the image contains patient identifiers.
  Just like the MinerU boundary, the provider stays fail-closed unless the
  backend-only ``ALLOW_IDENTIFIABLE_AI_VISION_UPLOAD`` switch is enabled and
  ``AI_VISION_VERIFICATION_ENABLED`` turns the capability on.
- **Constrained scope**: the verifier may only answer the exact missing
  fields that were requested. Any response field outside the request set
  rejects the whole verification (fail closed); it never re-interprets the
  record freely.
- **Missing stays missing**: an unclear read must return ``read_value=null``;
  a low-confidence read is treated as unread. Values are never guessed.
- **No bypass**: patched values re-enter the SAME validator, and the normal
  resolution chain (DrugDictionary, dose arithmetic, PatientMatcher) is
  untouched. Provenance is recorded per field with
  ``source=ai_visual_verification``.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, field_validator

from app.core.config import get_settings
from app.services.parsers.types import ParsedDocument

logger = logging.getLogger(__name__)

SOURCE_AI_VISUAL_VERIFICATION = "ai_visual_verification"

# Validator blocking codes that by construction mean the OCR SOURCE lost
# information (not that the AI misread a present value). Fail closed: codes
# not listed here are never vision-verified.
VISION_ELIGIBLE_ISSUE_CODES = (
    "PACKAGE_UNIT_MISSING_IN_OCR",
    "MEDICAL_RECORD_NUMBER_MISSING",
)

PACKAGE_UNITS = {"包": "package", "袋": "package"}
_MRN_PATTERN = re.compile(r"\d{4,20}")


class VisionVerificationRejected(ValueError):
    """The verification response violated the constrained contract."""


@dataclass(frozen=True)
class VisionFieldRequest:
    """One constrained question about one missing OCR field."""

    field_path: str
    kind: str
    question: str
    context: str | None = None


def build_vision_field_requests(
    parsed: ParsedDocument, issue_codes: list[str]
) -> list[VisionFieldRequest]:
    """Build the per-field questions for the eligible blocking codes only."""

    codes = set(issue_codes)
    requests: list[VisionFieldRequest] = []
    if "PACKAGE_UNIT_MISSING_IN_OCR" in codes:
        for index, item in enumerate(parsed.prescription_items):
            if item.package_count is None or item.original_package_unit is not None:
                continue
            requests.append(
                VisionFieldRequest(
                    field_path=f"prescription.items[{index}].original_package_unit",
                    kind="package_unit",
                    context=item.original_text,
                    question=(
                        "请仅核对此处方项在原始图片中，包装单位是否明确印为“包”或“袋”。"
                        "若图片中无法明确读出该单位，read_value 必须返回 null，不得猜测。"
                    ),
                )
            )
    if "MEDICAL_RECORD_NUMBER_MISSING" in codes:
        requests.append(
            VisionFieldRequest(
                field_path="patient_identity.medical_record_number",
                kind="medical_record_number",
                question=(
                    "请仅核对图片中明确标注为“病历号/门诊号/登记号/卡号”等标签字段的编号，"
                    "逐字返回其中的数字字符。若图片中没有带该类标签的编号或无法明确阅读，"
                    "read_value 必须返回 null，不得猜测，不得使用无标签的数字。"
                ),
            )
        )
    return requests


class VisionFieldVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_path: str
    read_value: str | None = None
    confidence: float
    evidence_quote: str | None = None

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be within 0 and 1")
        return value


class VisionVerificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verifications: list[VisionFieldVerification]


def apply_vision_verifications(
    parsed: ParsedDocument,
    requests: list[VisionFieldRequest],
    response: VisionVerificationResponse,
) -> tuple[ParsedDocument, list[dict[str, Any]]]:
    """Sanitize, apply, and record per-field verification outcomes.

    Returns the (possibly patched) document and the provenance records.
    Every requested field receives exactly one record; ``applied`` marks
    whether a sanitized value actually entered the candidate facts.
    """

    requested = {request.field_path: request for request in requests}
    seen: set[str] = set()
    for verification in response.verifications:
        if verification.field_path not in requested:
            raise VisionVerificationRejected(
                f"verification answered a field that was not requested: "
                f"{verification.field_path}"
            )
        if verification.field_path in seen:
            raise VisionVerificationRejected(
                f"duplicate verification for field: {verification.field_path}"
            )
        seen.add(verification.field_path)

    floor = get_settings().ai_vision_confidence_floor
    records: list[dict[str, Any]] = []
    items = list(parsed.prescription_items)
    medical_record_number = parsed.medical_record_number
    for request in requests:
        match = next(
            (v for v in response.verifications if v.field_path == request.field_path),
            None,
        )
        read_value = match.read_value if match is not None else None
        confidence = match.confidence if match is not None else 0.0
        evidence = match.evidence_quote if match is not None else None
        applied = False
        sanitized: str | None = None
        if read_value is not None and confidence >= floor:
            candidate = str(read_value).strip()
            if request.kind == "package_unit":
                # The source unit vocabulary is closed; anything else is
                # an unclear read and stays missing.
                sanitized = candidate if candidate in PACKAGE_UNITS else None
            elif request.kind == "medical_record_number":
                digits = re.sub(r"[\s-]", "", candidate)
                # Masked identities and non-plain numbers are never identity.
                if "*" in candidate or not _MRN_PATTERN.fullmatch(digits):
                    sanitized = None
                else:
                    sanitized = digits
            if sanitized is not None:
                applied = True
                if request.kind == "package_unit":
                    index = int(request.field_path.split("[")[1].split("]")[0])
                    item = items[index]
                    items[index] = type(item)(
                        original_text=item.original_text,
                        is_basic_drug=item.is_basic_drug,
                        raw_drug_name=item.raw_drug_name,
                        processing_method=item.processing_method,
                        explicit_package_dose_g=item.explicit_package_dose_g,
                        package_count=item.package_count,
                        original_package_unit=sanitized,
                        normalized_package_unit=PACKAGE_UNITS[sanitized],
                        is_direct_gram_dose=item.is_direct_gram_dose,
                        suggested_canonical_name=item.suggested_canonical_name,
                    )
                elif request.kind == "medical_record_number":
                    medical_record_number = sanitized
        records.append(
            {
                "field_path": request.field_path,
                "kind": request.kind,
                "source": SOURCE_AI_VISUAL_VERIFICATION,
                "read_value": sanitized,
                "raw_read_value": read_value,
                "applied": applied,
                "confidence": confidence,
                "evidence_quote": evidence,
            }
        )

    patched = replace(
        parsed,
        prescription_items=items,
        medical_record_number=medical_record_number,
        vision_verifications=[*parsed.vision_verifications, *records],
    )
    return patched, records


class VisionVerificationProviderError(RuntimeError):
    """The vision provider call failed; the document stays unresolved."""


class LiveVisionVerificationProvider:
    """OpenAI-compatible image-input adapter for the constrained re-read.

    Deterministic settings (temperature 0, no sampling knobs); the image is
    sent as a base64 data URL; the strict Pydantic gates above remain the
    contract authority — provider JSON mode is necessary but never
    sufficient. Constructor overrides come from the runtime
    ``provider_settings`` slot ``vision_verification`` that the settings UI
    maintains; without overrides the environment settings apply and the
    backend-only upload switch is mandatory.
    """

    name = "live_openai_compatible"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self._base_url = base_url
        self._model = model
        self._api_key = api_key

    def _resolved(self) -> tuple[str, str, str]:
        settings = get_settings()
        api_key = self._api_key or settings.ai_vision_provider_api_key
        if not api_key:
            raise VisionVerificationProviderError(
                "vision provider api key is not configured"
            )
        base_url = self._base_url or settings.ai_vision_provider_base_url
        model = self._model or settings.ai_vision_provider_model
        return base_url.rstrip("/"), model, api_key

    def describe(self) -> tuple[str, str]:
        """Provider name and resolved model, for records/audit (no secrets)."""

        _, model, _ = self._resolved()
        return self.name, model

    def verify_fields(
        self, image_bytes: bytes, image_media_type: str, requests: list[VisionFieldRequest]
    ) -> dict[str, Any]:
        settings = get_settings()
        # Environment path requires the explicit backend switch; a
        # runtime-configured provider (UI slot) already carries the user's
        # explicit image-upload confirmation and is audited.
        if not (self._api_key or settings.allow_identifiable_ai_vision_upload):
            raise VisionVerificationProviderError(
                "identifiable vision input is disabled by backend policy"
            )
        base_url, model, api_key = self._resolved()

        question_lines = [
            "你是病历图片的字段核对器。只回答下面明确列出的字段，"
            "不得解释或抽取任何其他内容。",
            "无法从图片中明确读出时 read_value 必须为 null。",
            "输出 JSON 结构：{\"verifications\":[{\"field_path\":\"...\","
            "\"read_value\":\"str|null\",\"confidence\":0-1,"
            "\"evidence_quote\":\"str|null\"}]}",
            "待核对字段：",
        ]
        for request in requests:
            line = f"- field_path={request.field_path}：{request.question}"
            if request.context:
                line += f"（OCR 原文片段：{request.context}）"
            question_lines.append(line)
        data_url = (
            f"data:{image_media_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        )
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "\n".join(question_lines)},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ]},
            ],
        }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=settings.ai_vision_provider_timeout_seconds) as client:
                with_json_mode = {**payload, "response_format": {"type": "json_object"}}
                response = client.post(
                    f"{base_url}/chat/completions", headers=headers, json=with_json_mode
                )
                if response.status_code == 400:
                    response = client.post(
                        f"{base_url}/chat/completions", headers=headers, json=payload
                    )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise VisionVerificationProviderError(
                f"vision provider HTTP error {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise VisionVerificationProviderError(
                f"vision provider transport error: {type(exc).__name__}"
            ) from exc
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise VisionVerificationProviderError(
                "vision provider reply shape was not a chat completion"
            ) from exc
        if not isinstance(content, str):
            raise VisionVerificationProviderError("vision provider reply had no message content")
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match is None:
            raise VisionVerificationProviderError("vision provider reply had no JSON object")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise VisionVerificationProviderError(
                "vision provider reply was not valid JSON"
            ) from exc


def get_vision_verification_provider() -> LiveVisionVerificationProvider | None:
    """Resolve the configured vision provider, or ``None`` while inactive.

    Priority mirrors the extraction provider: the environment-configured
    bootstrap path (``AI_VISION_PROVIDER_*`` + the backend-only upload
    switch), then the runtime ``provider_settings`` slot
    ``vision_verification`` maintained by the settings UI, else ``None`` —
    in which case the fallback silently does not run. A database lookup
    failure never breaks the pipeline.
    """

    settings = get_settings()
    if not settings.ai_vision_verification_enabled:
        return None
    if settings.ai_vision_provider_api_key and settings.allow_identifiable_ai_vision_upload:
        return LiveVisionVerificationProvider()
    try:
        from app.db.session import SessionLocal
        from app.services.ai.provider_config import (
            VISION_PROVIDER_SLOT,
            resolve_provider_params,
        )

        with SessionLocal() as session:
            params = resolve_provider_params(session, VISION_PROVIDER_SLOT)
        if params is not None:
            if params.get("confirmed_identifiable_upload") is not True:
                # The slot's stored privacy decision is missing; the image
                # boundary stays closed (fail-closed, same as unconfigured).
                logger.warning(
                    "Vision provider slot lacks the explicit identifiable-upload "
                    "confirmation; fallback stays inactive"
                )
                return None
            return LiveVisionVerificationProvider(
                base_url=params["base_url"],
                model=params["model"],
                api_key=params["api_key"],
            )
    except Exception:  # noqa: BLE001 - config lookup must never break ingestion
        logger.warning(
            "Vision provider settings lookup failed; fallback stays inactive",
            exc_info=True,
        )
    return None


def parse_vision_verification_response(payload: Any) -> VisionVerificationResponse:
    """Strict schema gate for the provider answer."""

    try:
        return VisionVerificationResponse.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError and shape errors
        raise VisionVerificationRejected(f"vision verification schema rejected: {exc}") from exc
