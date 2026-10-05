from __future__ import annotations

import json
import uuid

import pytest

from app.services.calibration import (
    ERROR_TAXONOMY,
    GoldenCaseReference,
    aggregate_benchmark_metrics,
    build_assisted_gold_draft,
    compare_clinical_benchmark,
    compare_expected_fields,
    evaluate_production_gate,
    has_suspicious_prescription_residual,
    load_selected_golden_cases,
    verify_assisted_candidate_snapshot,
)


def test_private_golden_manifest_requires_explicit_case_selection_and_safe_paths(tmp_path) -> None:
    root = tmp_path / "clinical_golden"
    images = root / "images"
    gold = root / "gold"
    images.mkdir(parents=True)
    gold.mkdir()
    (images / "case-001.jpg").write_bytes(b"private-test-image")
    (gold / "case-001.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "expected": {"hospital": "JNU", "total_decoctions": 7},
            }
        ),
        encoding="utf-8",
    )
    document_id = uuid.uuid4()
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "cases": [
                    {
                        "case_id": "case-001",
                        "document_id": str(document_id),
                        "image": "images/case-001.jpg",
                        "gold": "gold/case-001.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="explicit case ID"):
        load_selected_golden_cases(manifest, root, [])

    cases = load_selected_golden_cases(manifest, root, ["case-001"])
    assert len(cases) == 1
    assert cases[0].document_id == document_id
    comparison = compare_expected_fields(
        cases[0].expected,
        {"hospital": "JNU", "total_decoctions": 7, "patient_name": "not compared"},
    )
    assert comparison["matched_fields"] == 2
    assert comparison["mismatched_fields"] == 0


def test_private_golden_manifest_rejects_path_escape(tmp_path) -> None:
    root = tmp_path / "clinical_golden"
    root.mkdir()
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")
    gold = root / "gold.json"
    gold.write_text(json.dumps({"schema_version": "1.0", "expected": {}}), encoding="utf-8")
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "cases": [
                    {
                        "case_id": "escape",
                        "document_id": str(uuid.uuid4()),
                        "image": "../outside.jpg",
                        "gold": "gold.json",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="escapes the private root"):
        load_selected_golden_cases(manifest, root, ["escape"])


def test_critical_false_auto_accept_is_reported_without_clinical_values() -> None:
    expected = {
        "patient_identity": {"medical_record_number": "MRN-GOLD"},
        "encounter": {"encounter_date": "2026-08-01"},
        "prescription": {
            "item_count": 1,
            "items": [
                {
                    "canonical_drug_name": "鳖甲",
                    "explicit_package_dose_g": "10",
                    "package_count": "2",
                    "calculated_dose_g": "20",
                }
            ],
        },
    }
    actual = {
        "patient_identity": {"medical_record_number": "MRN-WRONG"},
        "encounter": {"encounter_date": "2026-08-01"},
        "prescription": {
            "item_count": 1,
            "items": [
                {
                    "canonical_drug_name": "鳖甲",
                    "explicit_package_dose_g": "5",
                    "package_count": "2",
                    "calculated_dose_g": "10",
                }
            ],
        },
        "validation": {"decision": "auto_accept"},
    }

    comparison = compare_clinical_benchmark(expected, actual)

    assert comparison["false_auto_accept"] is True
    assert comparison["critical_mismatched_fields"] == 3
    assert all(entry["error_category"] in ERROR_TAXONOMY for entry in comparison["mismatches"])
    serialized = json.dumps(comparison, ensure_ascii=False)
    assert "MRN-GOLD" not in serialized
    assert "MRN-WRONG" not in serialized


def test_missing_canonical_drug_is_classified_as_reference_data_missing() -> None:
    comparison = compare_clinical_benchmark(
        {"prescription": {"items": [{"raw_drug_name": "测试药", "canonical_drug_name": "测试药"}]}},
        {
            "prescription": {"items": [{"raw_drug_name": "测试药", "canonical_drug_name": None}]},
            "validation": {"decision": "unresolved"},
        },
    )

    assert comparison["mismatches"] == [
        {
            "field_path": "prescription.items[0].canonical_drug_name",
            "layer": "CLINICAL_NORMALIZATION",
            "critical": True,
            "error_category": "REFERENCE_DATA_MISSING",
            "classification_source": "automatic",
        }
    ]
    assert comparison["layers"]["CLINICAL_NORMALIZATION"]["critical_mismatched_fields"] == 1


def test_review_required_error_is_not_a_false_auto_accept_and_five_cases_never_pass_gate() -> None:
    comparison = compare_clinical_benchmark(
        {"prescription": {"item_count": 2}},
        {
            "prescription": {"item_count": 1},
            "validation": {"decision": "review_required"},
        },
    )
    assert comparison["false_auto_accept"] is False

    gate = evaluate_production_gate([comparison] * 5)
    assert gate["production_ready"] is False
    assert gate["status"] == "NOT_READY_INSUFFICIENT_REPRESENTATIVE_CASES"


def test_unparsed_prescription_residual_is_conservatively_reviewed() -> None:
    assert has_suspicious_prescription_residual("共7剂") is False
    assert has_suspicious_prescription_residual("每日1剂，水煎服") is False
    assert has_suspicious_prescription_residual("某药（10g）2包") is True


def test_merged_instruction_lines_are_folded_before_residual_decision() -> None:
    # Phase 6.1: OCR/extractor may merge instruction clauses onto one line;
    # folding splits clauses so known instruction forms stay explained.
    merged = "共7剂 用法:口服(中药) 每日1剂 每日2次 一次用量:150ml"
    assert has_suspicious_prescription_residual(merged) is False
    # Any clause that is not a known instruction still fails closed.
    assert has_suspicious_prescription_residual("共7剂 某药（10g）2包") is True
    assert has_suspicious_prescription_residual("茯神 （5g） 3包") is True


def test_spaced_instruction_phrases_are_rejoined_before_residual_decision() -> None:
    # Expansion CASE023: OCR spaces may shatter one phrase into fragments
    # ("共 7 付(13味)" -> 共 / 7 / 付(13味)); an unexplained clause is
    # re-joined with its successor. Start-anchored safe patterns can only be
    # satisfied by a benign phrase prefix, never by drug residue.
    spaced = "共 7 付(13味) 用法: 口服 每日1剂 每日2次 一次用量:150ml"
    assert has_suspicious_prescription_residual(spaced) is False
    # A drug-residue clause can never become explained by re-joining.
    assert has_suspicious_prescription_residual("共 7 付 茯神 （5g） 3包") is True
    assert has_suspicious_prescription_residual("每日1剂 某 药 （10g） 2包") is True


def _item(name: str, dose: str = "10", count: str | None = "2") -> dict:
    return {
        "raw_drug_name": name,
        "canonical_drug_name": name,
        "processing_method": None,
        "explicit_package_dose_g": dose,
        "package_count": count,
        "calculated_dose_g": str(int(dose) * int(count)) if count else None,
    }


def test_prescription_item_order_is_not_a_difference() -> None:
    expected = {
        "prescription": {"item_count": 3, "items": [_item("药A"), _item("药B"), _item("药C")]}
    }
    shuffled = {
        "prescription": {"item_count": 3, "items": [_item("药C"), _item("药A"), _item("药B")]}
    }
    comparison = compare_clinical_benchmark(expected, shuffled)
    assert comparison["mismatched_fields"] == 0
    assert comparison["critical_mismatched_fields"] == 0
    assert len(comparison["semantic_item_alignment"]["matched_pairs"]) == 3


def test_embedded_processing_prefix_still_aligns_items() -> None:
    # Gold splits the printed form into raw name + processing; the pipeline
    # keeps the printed 炒酸枣仁. Alignment must tolerate the display form
    # while still reporting the real canonical/processing field differences.
    expected = {
        "prescription": {
            "items": [
                {
                    "raw_drug_name": "酸枣仁",
                    "canonical_drug_name": "酸枣仁",
                    "processing_method": "炒",
                    "explicit_package_dose_g": "10",
                    "package_count": "1",
                }
            ]
        }
    }
    actual = {
        "prescription": {
            "items": [
                {
                    "raw_drug_name": "炒酸枣仁",
                    "canonical_drug_name": None,
                    "processing_method": None,
                    "explicit_package_dose_g": "10",
                    "package_count": "1",
                }
            ]
        },
        "validation": {"decision": "unresolved"},
    }
    comparison = compare_clinical_benchmark(expected, actual)
    alignment = comparison["semantic_item_alignment"]
    assert list(alignment["matched_pairs"]) == [(0, 0)]
    assert alignment["missing_expected_indices"] == []
    assert alignment["unexpected_actual_indices"] == []
    mismatch_paths = [entry["field_path"] for entry in comparison["mismatches"]]
    assert "prescription.items[0].canonical_drug_name" in mismatch_paths
    assert "prescription.items[0].processing_method" in mismatch_paths
    # The raw display form is a SOURCE_EXTRACTION difference, never critical.
    raw_entry = next(
        entry for entry in comparison["mismatches"] if entry["field_path"].endswith("raw_drug_name")
    )
    assert raw_entry["layer"] == "SOURCE_EXTRACTION"
    assert raw_entry["critical"] is False


def test_ambiguous_duplicate_items_are_never_force_matched() -> None:
    # Same-name duplicates with distinct doses align uniquely via the
    # dose/package tiebreakers; reordering is not a difference.
    reordered = {
        "prescription": {
            "item_count": 2,
            "items": [_item("药A", dose="15"), _item("药A", dose="10")],
        },
        "validation": {"decision": "auto_accept"},
    }
    expected = {
        "prescription": {
            "item_count": 2,
            "items": [_item("药A", dose="10"), _item("药A", dose="15")],
        }
    }
    assert compare_clinical_benchmark(expected, reordered)["mismatched_fields"] == 0

    # Duplicates that tie on every alignment key (differing only in a field
    # outside the tiebreakers, e.g. 基药 marker) are ambiguous: forcing a
    # pairing could silently swap the compared values.
    def _tied(basic: bool) -> dict:
        return {**_item("药A", dose="10"), "is_basic_drug": basic}

    tied_expected = {"prescription": {"item_count": 2, "items": [_tied(True), _tied(False)]}}
    tied_actual = {
        "prescription": {"item_count": 2, "items": [_tied(False), _tied(True)]},
        "validation": {"decision": "auto_accept"},
    }
    comparison = compare_clinical_benchmark(tied_expected, tied_actual)
    alignment = comparison["semantic_item_alignment"]
    assert alignment["ambiguous"] is True
    assert alignment["ambiguity_code"] == "AMBIGUOUS_ITEM_MATCH"
    assert "prescription.item_alignment" in [
        entry["field_path"] for entry in comparison["mismatches"]
    ]
    # Ambiguity is a critical SAFETY_DECISION difference: an auto-accepted
    # case with ambiguous item matching counts as a false auto-accept.
    assert comparison["false_auto_accept"] is True


def test_missing_and_unexpected_items_are_critical_layer_b_differences() -> None:
    expected = {"prescription": {"item_count": 2, "items": [_item("药A"), _item("药B")]}}
    actual = {
        "prescription": {"item_count": 2, "items": [_item("药A"), _item("药D")]},
        "validation": {"decision": "unresolved"},
    }
    comparison = compare_clinical_benchmark(expected, actual)
    mismatch_paths = [entry["field_path"] for entry in comparison["mismatches"]]
    assert any(path.startswith("prescription.items[") for path in mismatch_paths)
    assert any(path.startswith("prescription.unexpected_items[") for path in mismatch_paths)
    assert comparison["false_auto_accept"] is False


def test_layers_split_source_normalization_and_safety() -> None:
    expected = {
        "patient_identity": {"medical_record_number": "MRN"},
        "prescription": {"items": [{"raw_drug_name": "药A", "canonical_drug_name": "药A"}]},
        "validation": {"decision": "auto_accept"},
    }
    actual = {
        "patient_identity": {"medical_record_number": "MRN"},
        "prescription": {"items": [{"raw_drug_name": "药A别名", "canonical_drug_name": "药A"}]},
        "validation": {"decision": "auto_accept"},
    }
    comparison = compare_clinical_benchmark(expected, actual)
    layers = comparison["layers"]
    assert layers["SOURCE_EXTRACTION"]["mismatched_fields"] >= 1
    assert layers["CLINICAL_NORMALIZATION"]["mismatched_fields"] == 0
    assert set(layers) == {"SOURCE_EXTRACTION", "CLINICAL_NORMALIZATION", "SAFETY_DECISION"}


def test_aggregate_metrics_are_reported_per_track_with_legacy_coverage() -> None:
    current_case = compare_clinical_benchmark(
        {"encounter": {"encounter_date": "2026-08-01"}},
        {"encounter": {"encounter_date": "2026-08-01"}, "validation": {"decision": "auto_accept"}},
        case_type="JNU_CURRENT_V1",
        expected_ingestion_decision="AUTO_ACCEPT",
    )
    legacy_case = compare_clinical_benchmark(
        {"encounter": {"encounter_date": "2026-08-01"}},
        {"encounter": {"encounter_date": "2026-08-01"}, "validation": {"decision": "unresolved"}},
        case_type="JNU_LEGACY_V1",
        expected_ingestion_decision="AUTO_ACCEPT_IF_SAFE",
    )
    report = aggregate_benchmark_metrics([current_case, legacy_case])
    assert set(report["tracks"]) == {"JNU_CURRENT_V1", "JNU_LEGACY_V1"}
    current_metrics = report["tracks"]["JNU_CURRENT_V1"]["metrics"]
    assert current_metrics["encounter_date_accuracy"]["value"] == 1.0
    assert current_metrics["auto_accept_precision"]["numerator"] == 1
    legacy_track = report["tracks"]["JNU_LEGACY_V1"]
    assert legacy_track["safe_automated_coverage"]["value"] == 0.0
    assert legacy_track["metrics"]["unresolved_recall"]["denominator"] == 0


def test_production_gate_v2_requires_ten_current_auto_accepted_cases() -> None:
    passing_current = [
        {
            "case_type": "JNU_CURRENT_V1",
            "benchmark_completed": True,
            "pipeline_decision_before_gold_comparison": "auto_accept",
            "false_auto_accept": False,
            "critical_mismatched_paths": [],
        }
    ] * 12
    gate = evaluate_production_gate(passing_current)
    assert gate["status"] == "NOT_READY_INSUFFICIENT_REPRESENTATIVE_CASES"
    assert gate["review_required_as_default_terminal_workflow"] is False
    assert gate["unresolved_cases_may_remain_without_manual_action"] is True

    enough = (
        passing_current
        + [
            {
                "case_type": "JNU_LEGACY_V1",
                "benchmark_completed": True,
                "pipeline_decision_before_gold_comparison": "unresolved",
                "false_auto_accept": False,
                "critical_mismatched_paths": [],
            }
        ]
        * 10
    )
    gate = evaluate_production_gate(enough)
    assert gate["current_auto_accepted_without_critical_error"] == 12
    assert gate["status"] == "CANDIDATE_REQUIRES_OPERATIONAL_SIGN_OFF"


def test_gate_fails_on_current_auto_accept_shortfall_even_with_many_cases() -> None:
    cases = [
        {
            "case_type": "JNU_CURRENT_V1",
            "benchmark_completed": True,
            "pipeline_decision_before_gold_comparison": "unresolved",
            "false_auto_accept": False,
            "critical_mismatched_paths": [],
        }
    ] * 25
    gate = evaluate_production_gate(cases)
    assert gate["status"] == "NOT_READY_INSUFFICIENT_CURRENT_AUTO_ACCEPTED_CASES"


def test_manifest_schema_3_requires_supported_case_type_and_decision(tmp_path) -> None:
    root = tmp_path
    images = root / "images"
    gold = root / "gold"
    images.mkdir(parents=True)
    gold.mkdir()
    (images / "case-9.jpg").write_bytes(b"private-test-image")
    (gold / "case-9.json").write_text(
        json.dumps({"schema_version": "3.0", "expected": {}}), encoding="utf-8"
    )
    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "3.0",
                "cases": [
                    {
                        "case_id": "case-9",
                        "document_id": str(uuid.uuid4()),
                        "image": "images/case-9.jpg",
                        "gold": "gold/case-9.json",
                        "case_type": "JNU_LEGACY_V1",
                        "expected_ingestion_decision": "AUTO_ACCEPT_IF_SAFE",
                        "test_intents": ["legacy_template", "historical_import"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    cases = load_selected_golden_cases(manifest, root, ["case-9"])
    assert cases[0].case_type == "JNU_LEGACY_V1"
    assert cases[0].expected_ingestion_decision == "AUTO_ACCEPT_IF_SAFE"
    assert cases[0].test_intents == ("legacy_template", "historical_import")

    for invalid in (
        {"case_type": "JNU_RETRO_V9"},
        {"expected_ingestion_decision": "ALWAYS_ACCEPT"},
    ):
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["cases"][0].update(invalid)
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        try:
            load_selected_golden_cases(manifest, root, ["case-9"])
        except ValueError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError(f"manifest accepted unsupported value: {invalid}")
        payload["cases"][0].pop(next(iter(invalid)))
        manifest.write_text(json.dumps(payload), encoding="utf-8")


def test_assisted_draft_preserves_frozen_candidate_lineage_without_claiming_gold(
    tmp_path,
) -> None:
    reference = GoldenCaseReference(
        case_id="CASE003",
        document_id=uuid.uuid4(),
        image_path=tmp_path / "CASE003.jpg",
        gold_path=tmp_path / "CASE003.json",
        case_role="follow_up",
        expected_match_case_id="CASE002",
        case_type="JNU_CURRENT_V1",
        expected_ingestion_decision="AUTO_ACCEPT_IF_SAFE",
        test_intents=("standard_template", "processing", "dose"),
    )
    candidate = {
        "patient_identity": {"real_name": "仅用于测试的虚构姓名"},
        "validation": {"decision": "review_required", "review_flags": ["UNKNOWN_DRUG"]},
    }

    payload = build_assisted_gold_draft(
        reference,
        candidate,
        run_id="run-20260829T000000Z-deadbeef",
        candidate_snapshot_reference="runs/run-id/CASE003/normalized_clinical_result.json",
        candidate_snapshot_sha256="a" * 64,
        created_at="2026-08-29T00:00:00+00:00",
    )

    assert payload["annotation"]["status"] == "draft"
    assert payload["annotation"]["entry_mode"] == "mineru_assisted_correction"
    assert payload["annotation"]["requires_full_human_verification"] is True
    assert payload["expected"] is candidate


def test_assisted_candidate_snapshot_integrity_is_enforced() -> None:
    annotation = {
        "entry_mode": "mineru_assisted_correction",
        "candidate_frozen": True,
        "candidate_run_id": "run-20260829T000000Z-deadbeef",
        "candidate_snapshot_reference": (
            "runs/run-20260829T000000Z-deadbeef/CASE003/normalized_clinical_result.json"
        ),
        "candidate_snapshot_sha256": "a" * 64,
    }

    verify_assisted_candidate_snapshot(
        annotation,
        run_id="run-20260829T000000Z-deadbeef",
        actual_sha256="a" * 64,
    )

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        verify_assisted_candidate_snapshot(
            annotation,
            run_id="run-20260829T000000Z-deadbeef",
            actual_sha256="b" * 64,
        )

    with pytest.raises(ValueError, match="different run"):
        verify_assisted_candidate_snapshot(
            annotation,
            run_id="run-20260829T111111Z-feedface",
            actual_sha256="a" * 64,
        )
