"""Live structured-output provider adapter for the ClinicalAIExtractor (Phase 6.1).

OpenAI-compatible chat-completions adapter used only for validation and,
after OD-010 closes, approved operation. Non-negotiable properties:

- **Explicit privacy boundary**: the extraction input is real OCR text and
  therefore contains patient identifiers. Just like the MinerU boundary, the
  provider stays fail-closed until the backend-only
  ``AI_EXTRACTION_ALLOW_IDENTIFIABLE_INPUT`` switch is explicitly enabled.
- **Deterministic settings**: temperature 0 and no sampling knobs; the same
  OCR input must yield core-identical critical fields across runs.
- **Structured output**: the provider is asked for a JSON object
  (``response_format={"type": "json_object"}``; one retry without it for
  providers that reject the parameter). The strict Pydantic gates in
  ``validate_clinical_extraction`` remain the real schema authority —
  provider JSON mode is necessary but never sufficient.
- **No clinical logging**: request bodies, API keys, and OCR text are never
  logged; failures surface as redacted issue codes.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from app.core.config import get_settings
from app.services.ai.extractor import (
    ISSUE_PROVIDER_FAILED,
    ClinicalExtractionRejected,
)

logger = logging.getLogger(__name__)

_RULES = (
    "你是中医门诊病历的结构化信息抽取器。只做抽取，不做任何医学推断。",
    "规则（违反任何一条都会被拒绝）：",
    "1. 只提取 OCR 文本中真实存在的信息。字段在原文中不存在时必须输出 null，",
    "   绝不允许根据常识、常见规格或上下文补全。",
    "2. prescription.items 中每一项的 original_text 必须是 OCR 文本中原样出现的",
    "   完整条目文本（例如：（基）醋鳖甲（10g）2包 或 金银花 10g）。",
    "3. 药材名拆分：raw_drug_name 是去掉炮制括号后的药名，processing_method 是",
    "   炮制法（如 麸炒/醋制），没有则 null。",
    "4. 剂量只提取原文明示的数值：explicit_package_dose_g 是括号中明示的每包克数；",
    "   package_count 是明示的包/袋数量；package_unit 只能是 包 或 袋。",
    "   不要计算乘积，不要猜测。",
    "5. 旧版处方笺（中药处方笺/Rp）中直接印刷的每味克数（如 金银花 10g）：",
    "   explicit_package_dose_g=10，package_count=null，package_unit=null。",
    "6. suggested_canonical_name 可选：当你认为该药名对应某个规范药名时可提出，",
    "   但最终以词典为准；不确定就 null。",
    "7. 不要输出 schema 之外的任何字段。只输出一个 JSON 对象，不要 Markdown 代码块。",
    "8. extraction_warnings 只能使用 MULTIPLE_PATIENT_NAMES_SUSPECTED 或",
    "   UNLABELED_HEADER_MRN_INFERRED；没有则空数组。",
    "9. confidence 是你对整体抽取的置信度 0-1；任何字段看不清、不确定时降低它，",
    "   不要猜测。",
)

_SCHEMA_HINT = json.dumps(
    {
        "patient": {
            "real_name": "str|null",
            "sex": "male|female|other|unknown",
            "age": "int|null",
            "medical_record_number": "str|null",
        },
        "encounter": {
            "hospital": "str|null",
            "department": "str|null",
            "physician": "str|null",
            "encounter_date": "YYYY-MM-DD|null",
            "chief_complaint": "str|null",
            "present_illness": "str|null",
            "past_history": "str|null",
            "allergy": "str|null",
            "vital_signs": "str|null",
            "advice": "str|null",
            "treatment_record": "str|null",
        },
        "symptoms": [
            {
                "text": "str",
                "status": "present|absent|unknown",
                "trend": "new|persistent|improved|worsened|resolved|null",
            }
        ],
        "diagnoses": [
            {
                "text": "str",
                "diagnosis_type": "western|tcm_disease|tcm_pattern|procedure_status|problem",
                "is_primary": "bool",
            }
        ],
        "prescription": {
            "total_decoctions": "int|null",
            "raw_text": "str|null",
            "items": [
                {
                    "original_text": "str",
                    "raw_drug_name": "str",
                    "processing_method": "str|null",
                    "suggested_canonical_name": "str|null",
                    "is_basic_drug": "bool",
                    "explicit_package_dose_g": "number|null",
                    "package_count": "number|null",
                    "package_unit": "包|袋|null",
                    "confidence": "number",
                }
            ],
        },
        "extraction_warnings": ["str"],
        "confidence": "number",
    },
    ensure_ascii=False,
)

_RAW_TEXT_RULE = (
    "prescription.raw_text 只放处方区中未被识别为条目的用法/煎服说明行",
    "（如 共7剂、每日1剂、每日2次、一次150ml），不要放入药物条目行。"
)

SYSTEM_PROMPT = "\n".join((*_RULES, "输出 JSON 的结构：", _SCHEMA_HINT, *_RAW_TEXT_RULE))


def _strip_code_fences(text: str) -> str:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise ClinicalExtractionRejected(
            "provider reply contained no JSON object",
            issue_code=ISSUE_PROVIDER_FAILED,
        )
    return match.group(0)


class LiveOpenAICompatibleProvider:
    """OpenAI-compatible structured-output adapter (temperature 0).

    Parameters come either from environment settings or, when constructed
    with overrides, from the runtime ``provider_settings`` slot that the
    settings UI maintains. Overrides win; nothing is logged.
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
        base_url = self._base_url or settings.ai_provider_base_url
        model = self._model or settings.ai_provider_model
        api_key = self._api_key or settings.ai_provider_api_key
        if not api_key:
            raise ClinicalExtractionRejected(
                "live provider api key is not configured",
                issue_code=ISSUE_PROVIDER_FAILED,
            )
        return base_url.rstrip("/"), model, api_key

    def extract(self, ocr_text: str) -> dict[str, Any]:
        settings = get_settings()
        if not (
            self._api_key or settings.ai_extraction_allow_identifiable_input
        ):
            # Environment path requires the explicit backend switch; a
            # runtime-configured provider (UI slot) already carries the
            # user's explicit confirmation and is audited.
            raise ClinicalExtractionRejected(
                "identifiable AI extraction input is disabled by backend policy",
                issue_code=ISSUE_PROVIDER_FAILED,
            )
        base_url, model, api_key = self._resolved()
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": ocr_text},
            ],
        }

        try:
            with httpx.Client(timeout=settings.ai_provider_timeout_seconds) as client:
                with_json_mode = {**payload, "response_format": {"type": "json_object"}}
                response = client.post(
                    f"{base_url}/chat/completions", headers=headers, json=with_json_mode
                )
                if response.status_code == 400:
                    # Some OpenAI-compatible providers reject response_format;
                    # retry once with the same strict instruction only.
                    response = client.post(
                        f"{base_url}/chat/completions", headers=headers, json=payload
                    )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ClinicalExtractionRejected(
                f"provider HTTP error {exc.response.status_code}",
                issue_code=ISSUE_PROVIDER_FAILED,
            ) from exc
        except httpx.HTTPError as exc:
            raise ClinicalExtractionRejected(
                f"provider transport error: {type(exc).__name__}",
                issue_code=ISSUE_PROVIDER_FAILED,
            ) from exc

        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise ClinicalExtractionRejected(
                "provider reply shape was not a chat completion",
                issue_code=ISSUE_PROVIDER_FAILED,
            ) from exc
        if not isinstance(content, str):
            # A null/non-text content means the provider produced no
            # structured answer; fail closed with a provider-failure code
            # instead of an unexpected TypeError downstream.
            raise ClinicalExtractionRejected(
                "provider reply contained no message content",
                issue_code=ISSUE_PROVIDER_FAILED,
            )

        try:
            return json.loads(_strip_code_fences(content))
        except json.JSONDecodeError as exc:
            raise ClinicalExtractionRejected(
                "provider reply was not valid JSON",
                issue_code=ISSUE_PROVIDER_FAILED,
            ) from exc

    def verify(
        self,
        ocr_text: str,
        payload: dict[str, Any],
        blocking_issue_codes: list[str],
    ) -> dict[str, Any] | None:
        # Phase 6.1 keeps the chain single-pass; the verification pass stays
        # disabled until it is separately validated.
        return None
