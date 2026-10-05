from __future__ import annotations

import json
from pathlib import Path

from app.services.validation_policy import (
    REASON_CATEGORIES,
    REVIEW_REASON_POLICY,
    VALIDATION_POLICY_VERSION,
    analyze_review_reasons,
    classify_review_issue,
    decide_ingestion,
)


def test_reference_data_limitations_do_not_block_auto_accept() -> None:
    decision = decide_ingestion(
        ["HOSPITAL_SPECIFICATION_UNKNOWN", "SEX_UNKNOWN", "AGE_OUT_OF_RANGE"]
    )
    assert decision.decision == "auto_accept"
    assert decision.blocking_reasons == ()
    assert set(decision.non_blocking_warnings) == {
        "HOSPITAL_SPECIFICATION_UNKNOWN",
        "SEX_UNKNOWN",
        "AGE_OUT_OF_RANGE",
    }
    assert decision.validation_policy_version == VALIDATION_POLICY_VERSION


def test_dictionary_coverage_gap_is_non_blocking_and_pending() -> None:
    decision = decide_ingestion(
        ["UNKNOWN_DRUG", "HOSPITAL_SPECIFICATION_UNKNOWN", "SEX_UNKNOWN", "AGE_OUT_OF_RANGE"]
    )
    assert decision.decision == "auto_accept"
    assert decision.blocking_reasons == ()
    assert set(decision.non_blocking_warnings) == {
        "UNKNOWN_DRUG",
        "HOSPITAL_SPECIFICATION_UNKNOWN",
        "SEX_UNKNOWN",
        "AGE_OUT_OF_RANGE",
    }
    assert decision.validation_policy_version == VALIDATION_POLICY_VERSION


def test_unverifiable_drug_identity_blocks_under_phase_6_1() -> None:
    decision = decide_ingestion(["DRUG_IDENTITY_UNVERIFIABLE", "HOSPITAL_SPECIFICATION_UNKNOWN"])
    assert decision.decision == "unresolved"
    assert decision.blocking_reasons == ("DRUG_IDENTITY_UNVERIFIABLE",)
    assert decision.non_blocking_warnings == ("HOSPITAL_SPECIFICATION_UNKNOWN",)


def test_any_blocking_issue_routes_to_unresolved() -> None:
    decision = decide_ingestion(
        [
            "DRUG_IDENTITY_UNVERIFIABLE",
            "UNLABELED_HEADER_MRN_INFERRED",
            "PACKAGE_UNIT_MISSING_IN_OCR",
        ]
    )
    assert decision.decision == "unresolved"
    assert set(decision.blocking_reasons) == {
        "DRUG_IDENTITY_UNVERIFIABLE",
        "UNLABELED_HEADER_MRN_INFERRED",
        "PACKAGE_UNIT_MISSING_IN_OCR",
    }
    assert decision.non_blocking_warnings == ()


def test_unknown_issue_codes_fail_closed_as_blocking() -> None:
    rule = classify_review_issue("SOME_FUTURE_CODE")
    assert rule.blocking is True
    assert rule.criticality == "critical"
    assert rule.category == "OTHER"
    assert decide_ingestion(["SOME_FUTURE_CODE"]).decision == "unresolved"


def test_every_policy_rule_uses_known_categories_and_blocking_implies_critical() -> None:
    for rule in REVIEW_REASON_POLICY.values():
        assert rule.category in REASON_CATEGORIES
        assert rule.criticality in {"critical", "non_critical"}
        assert (rule.criticality == "critical") == rule.blocking


def test_web_reason_translations_cover_every_blocking_policy_code() -> None:
    translations_path = (
        Path(__file__).resolve().parents[3]
        / "apps"
        / "web"
        / "lib"
        / "ingestion-reasons.json"
    )
    translations = json.loads(translations_path.read_text(encoding="utf-8"))
    blocking_codes = {code for code, rule in REVIEW_REASON_POLICY.items() if rule.blocking}
    assert blocking_codes <= translations.keys()
    for code in blocking_codes:
        assert set(translations[code]) == {"title", "happened", "database", "action"}
        assert all(str(value).strip() for value in translations[code].values())


def test_blocking_rules_cover_patient_identity_dose_and_completeness() -> None:
    blocking_codes = {code for code, rule in REVIEW_REASON_POLICY.items() if rule.blocking}
    assert {
        "PATIENT_NAME_MISSING",
        "MEDICAL_RECORD_NUMBER_MISSING",
        "PATIENT_MATCH_REVIEW_REQUIRED",
        "PATIENT_IDENTITY_NAME_CONFLICT",
        "UNLABELED_HEADER_MRN_INFERRED",
        "PACKAGE_UNIT_MISSING_IN_OCR",
        "UNPARSED_PRESCRIPTION_RESIDUAL",
        "LEGACY_UNPARSED_RESIDUAL",
        "UNSUPPORTED_TEMPLATE",
        "LLM_STRUCTURING_SCHEMA_REJECTED",
        "LLM_STRUCTURING_LOW_CONFIDENCE",
        "DRUG_IDENTITY_UNVERIFIABLE",
        "ENCOUNTER_DATE_MISSING",
        "HOSPITAL_MISSING",
        "DUPLICATED_PRESCRIPTION_ITEM",
        "AI_EXTRACTION_PROVIDER_FAILED",
        "AI_EXTRACTION_SCHEMA_REJECTED",
        "AI_EXTRACTION_LOW_CONFIDENCE",
        "AI_EXTRACTION_EVIDENCE_MISSING",
    } <= blocking_codes
    assert "UNKNOWN_DRUG" not in blocking_codes


def test_analyze_review_reasons_reports_rule_field_criticality_blocking() -> None:
    analysis = analyze_review_reasons(
        [
            {
                "issue_code": "UNKNOWN_DRUG",
                "field_path": "prescription.items[3].canonical_drug_name",
            },
            {
                "issue_code": "HOSPITAL_SPECIFICATION_UNKNOWN",
                "field_path": "prescription.items[3].calculated_dose_g",
            },
        ]
    )
    assert analysis[0] == {
        "review_rule_id": "UNKNOWN_DRUG",
        "trigger_field": "prescription.items[3].canonical_drug_name",
        "reason_category": "REFERENCE_DATA_LIMITATION",
        "criticality": "non_critical",
        "blocking": False,
        "reason": analysis[0]["reason"],
    }
    assert analysis[1]["blocking"] is False
    assert analysis[1]["reason_category"] == "REFERENCE_DATA_LIMITATION"
    serialized = json.dumps(analysis, ensure_ascii=False)
    assert "prescription" in serialized  # field paths only, never clinical values
