from __future__ import annotations

import pytest

from app.services.legacy_structuring import (
    LegacyStructuringRejected,
    MockLegacyStructuringProvider,
    validate_legacy_structuring,
)


def _payload(**overrides) -> dict:
    payload = {
        "real_name": "测试患者",
        "sex": "female",
        "age": 70,
        "encounter_date": "2026-06-05",
        "medical_record_number": "700001",
        "department": "中医科门诊",
        "physician": "测试医师",
        "diagnoses": ["测试病证"],
        "items": [
            {
                "original_text": "金银花 10g",
                "raw_drug_name": "金银花",
                "processing_method": None,
                "explicit_dose_g": "10",
                "package_count": None,
                "package_unit": None,
                "confidence": 0.97,
            }
        ],
        "total_decoctions": 7,
        "confidence": 0.95,
    }
    payload.update(overrides)
    return payload


def test_valid_structuring_payload_passes_strict_schema() -> None:
    document = validate_legacy_structuring(_payload())
    assert document.items[0].raw_drug_name == "金银花"


def test_extra_fields_are_rejected() -> None:
    payload = _payload()
    payload["free_text_interpretation"] = "医师认为患者应长期服药"
    with pytest.raises(LegacyStructuringRejected) as excinfo:
        validate_legacy_structuring(payload)
    assert excinfo.value.issue_code == "LLM_STRUCTURING_SCHEMA_REJECTED"


def test_masked_mrn_and_non_cjk_names_are_rejected() -> None:
    with pytest.raises(LegacyStructuringRejected):
        validate_legacy_structuring(_payload(medical_record_number="*04773423*"))
    payload = _payload()
    payload["items"][0]["raw_drug_name"] = "honeysuckle"
    with pytest.raises(LegacyStructuringRejected):
        validate_legacy_structuring(payload)


def test_low_confidence_output_is_rejected_regardless_of_schema_validity() -> None:
    payload = _payload(confidence=0.4)
    with pytest.raises(LegacyStructuringRejected) as excinfo:
        validate_legacy_structuring(payload)
    assert excinfo.value.issue_code == "LLM_STRUCTURING_LOW_CONFIDENCE"

    payload = _payload()
    payload["items"][0]["confidence"] = 0.2
    with pytest.raises(LegacyStructuringRejected) as excinfo:
        validate_legacy_structuring(payload)
    assert excinfo.value.issue_code == "LLM_STRUCTURING_LOW_CONFIDENCE"


def test_out_of_range_dose_and_missing_items_are_rejected() -> None:
    payload = _payload()
    payload["items"][0]["explicit_dose_g"] = "9000"
    with pytest.raises(LegacyStructuringRejected):
        validate_legacy_structuring(payload)
    with pytest.raises(LegacyStructuringRejected):
        validate_legacy_structuring(_payload(items=[]))


def test_mock_provider_returns_only_the_configured_payload() -> None:
    provider = MockLegacyStructuringProvider(_payload())
    assert provider.structure("ignored-ocr")["real_name"] == "测试患者"
    with pytest.raises(LegacyStructuringRejected):
        MockLegacyStructuringProvider().structure("ignored-ocr")


def test_structuring_stays_disabled_by_default(monkeypatch) -> None:
    from app.core.config import get_settings
    from app.services.legacy_structuring import get_legacy_structuring_provider

    get_settings.cache_clear()
    monkeypatch.setenv("LEGACY_LLM_STRUCTURING_ENABLED", "false")
    get_settings.cache_clear()
    assert get_legacy_structuring_provider() is None
