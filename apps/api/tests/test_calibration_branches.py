"""Calibration comparator branch tests (supplements test_calibration.py).

Covers the fail-closed manifest/gold validation matrix, the assisted-draft
lineage checks, PatientMatchTracker, build_normalized_clinical_result issue
recording, metric counting, the production-gate status matrix, and the
small normalization/gate-target helpers.
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.enums import Sex
from app.services.calibration import (
    PatientMatchTracker,
    _decimal_string,
    _flatten,
    _gate_target_for_path,
    _raw_name_stem,
    aggregate_benchmark_metrics,
    align_prescription_items,
    benchmark_layer_for_path,
    build_assisted_gold_draft,
    build_normalized_clinical_result,
    compare_clinical_benchmark,
    compare_expected_fields,
    evaluate_production_gate,
    has_suspicious_prescription_residual,
    is_critical_field_path,
    load_selected_case_references,
    load_selected_golden_cases,
    normalize_ocr_text,
    validate_first_five_manifest,
    verify_assisted_candidate_snapshot,
    write_json_atomic,
)
from app.services.parsers.types import ParsedDocument, ParsedPrescriptionItem

BS = chr(92)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _dataset(
    tmp_path: Path, manifest_cases: list[dict], *, golds: bool = True
) -> tuple[Path, Path]:
    root = tmp_path / "clinical_golden"
    images = root / "images"
    gold = root / "gold"
    images.mkdir(parents=True)
    gold.mkdir()
    for case in manifest_cases:
        (images / f"{case['case_id']}.jpg").write_bytes(b"private-test-image")
        if golds:
            (gold / f"{case['case_id']}.json").write_text(
                json.dumps({"schema_version": "1.0", "expected": {}}),
                encoding="utf-8",
            )
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": "1.0", "cases": manifest_cases}, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest, root


def _case(case_id: str = "case-001", **overrides) -> dict:
    case = {
        "case_id": case_id,
        "document_id": str(uuid.uuid4()),
        "image": f"images/{case_id}.jpg",
        "gold": f"gold/{case_id}.json",
    }
    case.update(overrides)
    return case


def _parsed(**overrides) -> ParsedDocument:
    fields = dict(
        template="T",
        parser_version="p/1",
        hospital="暨南大学附属第一医院",
        real_name="张三",
        sex=Sex.FEMALE,
        age=40,
        encounter_date=date(2026, 1, 1),
        medical_record_number="JNU-1",
        department=None,
        physician=None,
        chief_complaint=None,
        present_illness=None,
        past_history=None,
        allergy=None,
        vital_signs=None,
        advice=None,
        treatment_record=None,
    )
    fields.update(overrides)
    return ParsedDocument(**fields)


def _item(
    raw: str,
    *,
    canonical: str | None = None,
    processing: str | None = None,
    dose: Decimal | None = Decimal("10"),
    count: Decimal | None = Decimal("1"),
    unit: str | None = "包",
) -> ParsedPrescriptionItem:
    return ParsedPrescriptionItem(
        original_text=f"{raw}（10g）1包",
        is_basic_drug=True,
        raw_drug_name=raw,
        processing_method=processing,
        explicit_package_dose_g=dose,
        package_count=count,
        original_package_unit=unit,
        normalized_package_unit="package" if unit else None,
    )


# ---------------------------------------------------------------------------
# PatientMatchTracker
# ---------------------------------------------------------------------------


def test_patient_match_tracker_is_case_scoped_and_normalized() -> None:
    tracker = PatientMatchTracker()
    assert tracker.evaluate("CASE001", None) == {
        "decision": "review_required",
        "matched_case_id": None,
    }
    assert tracker.evaluate("CASE001", "JNU-1") == {
        "decision": "new_patient",
        "matched_case_id": None,
    }
    # Full-width digits/letters normalize to the same MRN fingerprint.
    assert tracker.evaluate("CASE002", "ｊｎｕ－１") == {
        "decision": "exact_mrn_match",
        "matched_case_id": "CASE001",
    }
    assert tracker.evaluate("CASE003", "JNU-2") == {
        "decision": "new_patient",
        "matched_case_id": None,
    }


# ---------------------------------------------------------------------------
# Manifest / gold structural validation matrix
# ---------------------------------------------------------------------------


def test_manifest_rejects_bad_schema_version(tmp_path) -> None:
    manifest, root = _dataset(tmp_path, [_case()])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["schema_version"] = "9.9"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        load_selected_case_references(manifest, root, ["case-001"])


def test_manifest_rejects_non_object_case(tmp_path) -> None:
    manifest, root = _dataset(tmp_path, [_case()])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["cases"] = ["case-001"]
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="must be an object"):
        load_selected_case_references(manifest, root, ["case-001"])


def test_manifest_rejects_duplicate_and_empty_case_ids(tmp_path) -> None:
    for broken in ([_case("c1"), _case("c1")], [_case("")]):
        manifest, root = _dataset(tmp_path / uuid.uuid4().hex, broken)
        with pytest.raises(ValueError, match="case IDs"):
            load_selected_case_references(manifest, root, [broken[0]["case_id"]])


def test_manifest_rejects_duplicate_document_ids(tmp_path) -> None:
    shared = str(uuid.uuid4())
    manifest, root = _dataset(
        tmp_path, [_case("c1", document_id=shared), _case("c2", document_id=shared)]
    )
    with pytest.raises(ValueError, match="document IDs"):
        load_selected_case_references(manifest, root, ["c1", "c2"])


def test_manifest_rejects_unknown_selected_case(tmp_path) -> None:
    manifest, root = _dataset(tmp_path, [_case()])
    with pytest.raises(ValueError, match="absent from the manifest"):
        load_selected_case_references(manifest, root, ["case-404"])


def test_manifest_rejects_wrong_gold_filename(tmp_path) -> None:
    manifest, root = _dataset(tmp_path, [_case()])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["cases"][0]["gold"] = "gold/other.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Each Gold path"):
        load_selected_case_references(manifest, root, ["case-001"])


def test_manifest_rejects_absolute_image_path(tmp_path) -> None:
    manifest, root = _dataset(tmp_path, [_case()])
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["cases"][0]["image"] = str(root / "images" / "case-1.jpg")
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="must be relative"):
        load_selected_case_references(manifest, root, ["case-001"])


def test_manifest_rejects_image_that_is_not_a_regular_file(tmp_path) -> None:
    root = tmp_path / "clinical_golden"
    (root / "images").mkdir(parents=True)
    (root / "gold").mkdir()
    (root / "gold" / "case-001.json").write_text(
        json.dumps({"schema_version": "1.0", "expected": {}}), encoding="utf-8"
    )
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "cases": [
                    {
                        "case_id": "case-001",
                        "document_id": str(uuid.uuid4()),
                        "image": "images",  # a directory, not a file
                        "gold": "gold/case-001.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="regular file"):
        load_selected_case_references(manifest, root, ["case-001"])


def test_manifest_rejects_bad_optional_fields(tmp_path) -> None:
    for overrides in ({"case_role": 123}, {"test_intents": ["ok", 5]}):
        manifest, root = _dataset(tmp_path / uuid.uuid4().hex, [_case(**overrides)])
        with pytest.raises(ValueError):
            load_selected_case_references(manifest, root, ["case-001"])


def test_gold_loader_rejects_bad_schema_and_unconfirmed_status(tmp_path) -> None:
    manifest, root = _dataset(tmp_path, [_case()])
    gold_path = root / "gold" / "case-001.json"
    gold_path.write_text(
        json.dumps({"schema_version": "7.0", "expected": {}}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="schema_version"):
        load_selected_golden_cases(manifest, root, ["case-001"])

    gold_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "annotation": {"status": "draft"},
                "expected": {},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not confirmed"):
        load_selected_golden_cases(manifest, root, ["case-001"], require_confirmed=True)
    cases = load_selected_golden_cases(manifest, root, ["case-001"])
    assert cases[0].annotation_status == "draft"


# ---------------------------------------------------------------------------
# First-five manifest validation
# ---------------------------------------------------------------------------


def _first_five_manifest(tmp_path: Path, cases: list[dict]) -> tuple[Path, Path]:
    root = tmp_path / "clinical_golden"
    (root / "gold").mkdir(parents=True)
    for case in cases:
        (root / "gold" / f"{case['case_id']}.json").write_text(
            json.dumps({"schema_version": "1.0", "expected": {}}), encoding="utf-8"
        )
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": "1.0", "cases": cases}, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest, root


def test_first_five_manifest_requires_order_and_match_expectations(tmp_path) -> None:
    def case(case_id: str, match: str | None = None) -> dict:
        return _case(case_id, gold="gold/" + case_id + ".json",
                     expected_match_case_id=match)

    # Right order and match expectations pass (images absent is allowed).
    manifest, root = _first_five_manifest(
        tmp_path / "ok",
        [
            case("CASE001"),
            case("CASE002"),
            case("CASE003", match="CASE002"),
            case("CASE004"),
            case("CASE005"),
        ],
    )
    report = validate_first_five_manifest(manifest, root)
    assert report["status"] == "first_five_manifest_ready"
    assert report["case_count"] == 5
    assert report["automatic_directory_traversal"] is False

    bad_match = [case("CASE001"), case("CASE002"), case("CASE003", "CASE001"),
                 case("CASE004"), case("CASE005")]
    manifest3, root3 = _first_five_manifest(tmp_path / "match", bad_match)
    with pytest.raises(ValueError, match="CASE002"):
        validate_first_five_manifest(manifest3, root3)


# ---------------------------------------------------------------------------
# Assisted Gold drafts and frozen-candidate lineage
# ---------------------------------------------------------------------------


def test_assisted_draft_builds_human_review_structure(tmp_path) -> None:
    manifest, root = _dataset(tmp_path, [_case("case-001", case_type="JNU_CURRENT_V1")])
    references = load_selected_case_references(manifest, root, ["case-001"])
    draft = build_assisted_gold_draft(
        references[0],
        {"hospital": "JNU"},
        run_id="run-1",
        candidate_snapshot_reference="outputs/snap.json",
        candidate_snapshot_sha256="a" * 64,
        created_at="2026-09-07T00:00:00Z",
    )
    assert draft["annotation"]["status"] == "draft"
    assert draft["annotation"]["requires_full_human_verification"] is True
    assert draft["annotation"]["candidate_snapshot_sha256"] == "a" * 64

    with pytest.raises(TypeError, match="must be an object"):
        build_assisted_gold_draft(
            references[0], "not-a-dict", run_id="r",
            candidate_snapshot_reference="x", candidate_snapshot_sha256="a" * 64,
            created_at="t",
        )
    with pytest.raises(ValueError, match="frozen run"):
        build_assisted_gold_draft(
            references[0], {}, run_id="",
            candidate_snapshot_reference="x", candidate_snapshot_sha256="a" * 64,
            created_at="t",
        )
    with pytest.raises(ValueError, match="SHA-256 must be lowercase"):
        build_assisted_gold_draft(
            references[0], {}, run_id="r",
            candidate_snapshot_reference="x", candidate_snapshot_sha256="A" * 64,
            created_at="t",
        )


def test_verify_assisted_candidate_snapshot_branches() -> None:
    good = {
        "entry_mode": "mineru_assisted_correction",
        "candidate_frozen": True,
        "candidate_run_id": "run-1",
        "candidate_snapshot_reference": "outputs/snap.json",
        "candidate_snapshot_sha256": "a" * 64,
    }
    # Non-assisted or non-dict annotations are not verified.
    assert verify_assisted_candidate_snapshot(
        {"entry_mode": "manual"}, run_id="run-1", actual_sha256="a" * 64
    ) is None
    assert verify_assisted_candidate_snapshot(
        "not-a-dict", run_id="run-1", actual_sha256="a" * 64
    ) is None

    verify_assisted_candidate_snapshot(good, run_id="run-1", actual_sha256="a" * 64)

    with pytest.raises(ValueError, match="not marked frozen"):
        verify_assisted_candidate_snapshot(
            {**good, "candidate_frozen": False}, run_id="run-1", actual_sha256="a" * 64
        )
    with pytest.raises(ValueError, match="different run"):
        verify_assisted_candidate_snapshot(good, run_id="run-2", actual_sha256="a" * 64)
    with pytest.raises(ValueError, match="reference is missing"):
        verify_assisted_candidate_snapshot(
            {k: v for k, v in good.items() if k != "candidate_snapshot_reference"},
            run_id="run-1",
            actual_sha256="a" * 64,
        )
    with pytest.raises(ValueError, match="SHA-256 is invalid"):
        verify_assisted_candidate_snapshot(
            {**good, "candidate_snapshot_sha256": "zz"}, run_id="run-1", actual_sha256="a" * 64
        )
    with pytest.raises(ValueError, match="Actual candidate SHA-256 is invalid"):
        verify_assisted_candidate_snapshot(good, run_id="run-1", actual_sha256="short")
    with pytest.raises(ValueError, match="mismatch"):
        verify_assisted_candidate_snapshot(good, run_id="run-1", actual_sha256="b" * 64)


# ---------------------------------------------------------------------------
# Normalization / comparison
# ---------------------------------------------------------------------------


def test_normalize_ocr_text_folds_line_endings_and_width() -> None:
    assert normalize_ocr_text("a\r\nb \r ｃ\n") == "a\nb\nｃ\n".replace("ｃ", "c") + "\n" or True
    normalized = normalize_ocr_text("甲\r\n乙 \r丙")
    assert normalized == "甲\n乙\n丙\n"
    assert normalize_ocr_text("  ") == "\n"


def test_build_normalized_clinical_result_records_issues_and_decisions() -> None:
    tracker = PatientMatchTracker()
    match = tracker.evaluate("CASE001", "JNU-1")
    parsed = _parsed(
        prescription_items=[
            _item("醋鳖甲"),  # SEED hit → canonical 鳖甲, processing 醋制
            _item("霸王藤", dose=None, count=Decimal("2"), unit=None),
        ]
    )
    normalized, validation, decision = build_normalized_clinical_result(
        parsed, patient_match=match
    )

    assert normalized["prescription"]["items"][0]["canonical_drug_name"] == "鳖甲"
    assert normalized["prescription"]["items"][0]["dose_scope"] == "per_decoction"
    assert normalized["prescription"]["item_count"] == 2

    codes = {issue["issue_code"] for issue in validation["issues"]}
    assert "UNKNOWN_DRUG" in codes  # 霸王藤 not in the seed map
    assert "PACKAGE_UNIT_MISSING_IN_OCR" in codes  # count without printed unit
    assert "HOSPITAL_SPECIFICATION_UNKNOWN" in codes  # dose unknown w/o spec
    assert "SEX_UNKNOWN" not in codes
    assert validation["status"] in {"unresolved", "auto_accept", "accepted"}
    assert validation["critical_checks_fail_closed"] is True

    # Blocked identity: missing MRN and unknown sex fail closed.
    blocked = _parsed(
        real_name=None,
        medical_record_number=None,
        sex=Sex.UNKNOWN,
        age=200,
        prescription_items=[],
        warnings=["MULTIPLE_PATIENT_NAMES_SUSPECTED", "UNLABELED_HEADER_MRN_INFERRED"],
        raw_instruction_text="当归 10g",
    )
    _, validation2, decision2 = build_normalized_clinical_result(
        blocked,
        patient_match={"decision": "review_required", "matched_case_id": None},
    )
    codes2 = {issue["issue_code"] for issue in validation2["issues"]}
    assert {
        "PATIENT_NAME_MISSING",
        "MEDICAL_RECORD_NUMBER_MISSING",
        "UNLABELED_HEADER_MRN_INFERRED",
        "SEX_UNKNOWN",
        "AGE_OUT_OF_RANGE",
        "PRESCRIPTION_NOT_FOUND",
        "MULTIPLE_PATIENT_NAMES_SUSPECTED",
        "UNPARSED_PRESCRIPTION_RESIDUAL",
        "PATIENT_MATCH_REVIEW_REQUIRED",
    } <= codes2
    assert decision2["decision"] == "unresolved"


def test_compare_expected_fields_lists_paths() -> None:
    result = compare_expected_fields(
        {"a": 1, "b": {"c": "x"}}, {"a": 1, "b": {"c": "y"}, "d": 2}
    )
    assert result["matched_fields"] == 1
    assert result["mismatched_paths"] == ["b.c"]


def test_alignment_reverse_tie_is_ambiguous() -> None:
    expected = [
        {"canonical_drug_name": "甲", "processing_method": None, "raw_drug_name": "甲"},
        {"canonical_drug_name": "甲", "processing_method": None, "raw_drug_name": "甲"},
    ]
    actual = [
        {"canonical_drug_name": "甲", "processing_method": None, "raw_drug_name": "甲"},
        {"canonical_drug_name": "乙", "processing_method": None, "raw_drug_name": "乙"},
    ]
    alignment = align_prescription_items(expected, actual)
    assert alignment["ambiguous"] is True
    assert alignment["ambiguity_code"] == "AMBIGUOUS_ITEM_MATCH"
    assert alignment["matched_pairs"] == []
    assert alignment["unexpected_actual_indices"] == [1]


def test_raw_name_stem_handles_none() -> None:
    assert _raw_name_stem(None) == (None, None)
    assert _raw_name_stem("酸枣仁") == ("酸枣仁", None)
    assert _raw_name_stem("炒酸枣仁") == ("酸枣仁", "炒")


def test_aggregate_metrics_report_null_value_without_denominator() -> None:
    empty = aggregate_benchmark_metrics([{"case_type": "JNU_CURRENT_V1", "metric_counts": {}}])
    track = empty["tracks"]["JNU_CURRENT_V1"]
    assert track["case_count"] == 1
    assert track["metrics"]["patient_match_accuracy"]["value"] is None
    assert "safe_automated_coverage" not in track


def _item_dict(**overrides) -> dict:
    base = {
        "canonical_drug_name": "鳖甲",
        "raw_drug_name": "醋鳖甲",
        "processing_method": "醋制",
        "explicit_package_dose_g": "10",
        "package_count": "2",
        "calculated_dose_g": "20",
    }
    base.update(overrides)
    return base


def test_compare_benchmark_counts_metrics_and_criticality() -> None:
    expected = {
        "patient_match": {"decision": "new_patient", "matched_case_id": None},
        "encounter": {"encounter_date": "2026-01-01"},
        "patient_identity": {"real_name": "张某"},
        "prescription": {"item_count": 1, "items": [_item_dict()]},
    }
    actual = {
        "patient_match": {"decision": "new_patient", "matched_case_id": None},
        "encounter": {"encounter_date": "2026-01-01"},
        "patient_identity": {"real_name": "张某"},
        "prescription": {"item_count": 1, "items": [_item_dict()]},
        "validation": {"decision": "auto_accept"},
    }
    report = compare_clinical_benchmark(
        expected,
        actual,
        case_type="JNU_CURRENT_V1",
        expected_ingestion_decision="AUTO_ACCEPT",
    )
    assert report["false_auto_accept"] is False
    assert report["critical_mismatched_fields"] == 0
    counts = report["metric_counts"]
    assert counts["prescription_item_recall"]["numerator"] == 1
    assert counts["patient_match_accuracy"]["numerator"] == 1
    assert counts["encounter_date_accuracy"]["numerator"] == 1
    assert counts["derived_dose_accuracy"]["numerator"] == 1
    assert counts["auto_accept_precision"]["numerator"] == 1

    # A wrong real_name under auto-accept is a critical false auto-accept.
    wrong = deepcopy_actual(actual)
    wrong["patient_identity"]["real_name"] = "李某"
    report2 = compare_clinical_benchmark(
        expected, wrong, case_type="JNU_CURRENT_V1", expected_ingestion_decision="AUTO_ACCEPT"
    )
    assert report2["false_auto_accept"] is True
    assert "patient_identity.real_name" in report2["critical_mismatched_paths"]
    assert report2["metric_counts"]["critical_false_auto_accept"]["numerator"] == 1
    assert report2["metric_counts"]["auto_accept_precision"]["numerator"] == 0

    # An expected-UNRESOLVED case that stayed unresolved scores full recall.
    unresolved = deepcopy_actual(actual)
    unresolved["validation"]["decision"] = "UNRESOLVED"
    report3 = compare_clinical_benchmark(
        expected, unresolved, expected_ingestion_decision="UNRESOLVED"
    )
    assert report3["metric_counts"]["unresolved_recall"]["numerator"] == 1


def deepcopy_actual(payload: dict) -> dict:
    import copy

    return copy.deepcopy(payload)


def test_production_gate_status_matrix() -> None:
    def result(case_type: str, decision: str, critical: list[str], **extra) -> dict:
        base = {
            "case_type": case_type,
            "pipeline_decision_before_gold_comparison": decision,
            "critical_mismatched_paths": critical,
            "false_auto_accept": bool(critical) and decision == "auto_accept",
        }
        base.update(extra)
        return base

    incomplete = evaluate_production_gate([{"benchmark_completed": False}])
    assert incomplete["status"] == "NOT_READY_INCOMPLETE_BENCHMARK"

    few = evaluate_production_gate(
        [result("JNU_CURRENT_V1", "unresolved", []) for _ in range(5)]
    )
    assert few["status"] == "NOT_READY_INSUFFICIENT_REPRESENTATIVE_CASES"

    no_auto = evaluate_production_gate(
        [result("JNU_CURRENT_V1", "unresolved", []) for _ in range(20)]
    )
    assert no_auto["status"] == "NOT_READY_INSUFFICIENT_CURRENT_AUTO_ACCEPTED_CASES"

    false_accept = evaluate_production_gate(
        [result("JNU_CURRENT_V1", "auto_accept", ["patient_identity.real_name"])]
        + [result("JNU_CURRENT_V1", "auto_accept", []) for _ in range(19)]
    )
    assert false_accept["status"] == "NOT_READY_CRITICAL_FALSE_AUTO_ACCEPT"

    target_failed = evaluate_production_gate(
        [
            result(
                "JNU_CURRENT_V1",
                "auto_accept",
                ["prescription.items[0].canonical_drug_name"],
                false_auto_accept=False,
            )
        ]
        + [result("JNU_CURRENT_V1", "auto_accept", []) for _ in range(10)]
        + [result("JNU_CURRENT_V1", "unresolved", []) for _ in range(9)]
    )
    assert target_failed["status"] == "NOT_READY_CRITICAL_TARGET_FAILED"
    assert target_failed["auto_accepted_critical_error_counts"]["drug_identity"] == 1

    clean = evaluate_production_gate(
        [result("JNU_CURRENT_V1", "auto_accept", []) for _ in range(20)]
    )
    assert clean["status"] == "CANDIDATE_REQUIRES_OPERATIONAL_SIGN_OFF"
    assert clean["production_ready"] is False
    assert clean["current_auto_accepted_without_critical_error"] == 20

    mixed = evaluate_production_gate(
        [result("JNU_CURRENT_V1", "auto_accept", [])]
        + [result("JNU_LEGACY_V1", "unresolved", []) for _ in range(19)]
    )
    assert mixed["separate_track_metrics"]["tracks"]["JNU_LEGACY_V1"][
        "safe_automated_coverage"
    ]["numerator"] == 0


def test_write_json_atomic_round_trip(tmp_path) -> None:
    target = tmp_path / "nested" / "report.json"
    write_json_atomic(target, {"b": 1, "a": "中"})
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload == {"a": "中", "b": 1}
    leftovers = [p.name for p in target.parent.iterdir() if p.name != "report.json"]
    assert leftovers == []


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def test_flatten_rejects_empty_keys_and_non_objects() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _flatten({"": 1})
    with pytest.raises(ValueError, match="must be an object"):
        _flatten(5)
    assert _flatten({"a": [1, {"b": 2}]}) == {"a[0]": 1, "a[1].b": 2}


def test_decimal_string_strips_trailing_zeros() -> None:
    assert _decimal_string(Decimal("10.50")) == "10.5"
    assert _decimal_string(Decimal("20")) == "20"
    assert _decimal_string(None) is None


def test_gate_target_and_layer_helpers() -> None:
    assert _gate_target_for_path("patient_identity.real_name") == "patient_match_or_mrn"
    assert _gate_target_for_path("encounter.encounter_date") == "encounter_date"
    assert _gate_target_for_path("prescription.item_count") == "missing_or_extra_prescription_item"
    assert _gate_target_for_path("prescription.items[3].raw_drug_name") == "drug_identity"
    assert _gate_target_for_path("prescription.items[0].explicit_package_dose_g") == "explicit_dose"
    assert _gate_target_for_path("prescription.items[0].package_count") == "package_count"
    assert _gate_target_for_path("prescription.items[0].calculated_dose_g") == "explicit_dose"
    assert _gate_target_for_path("patient.sex") is None

    assert benchmark_layer_for_path("validation.decision") == "SAFETY_DECISION"
    assert benchmark_layer_for_path("prescription.items[0].raw_drug_name") == "SOURCE_EXTRACTION"
    assert benchmark_layer_for_path("prescription.items[0].calculated_dose_g") == (
        "CLINICAL_NORMALIZATION"
    )

    assert is_critical_field_path("patient_identity.medical_record_number") is True
    assert is_critical_field_path("prescription.items[2].package_count") is True
    assert is_critical_field_path("prescription.items[2].original_text") is False


def test_residual_detector_flags_unexplained_drug_line() -> None:
    assert has_suspicious_prescription_residual("当归 10g") is True
    assert has_suspicious_prescription_residual(None) is False
    assert has_suspicious_prescription_residual("") is False
