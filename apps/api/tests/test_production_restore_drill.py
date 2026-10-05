"""Unit tests for the Phase 5.2 restore-drill check evaluation.

判定必须逐项为 True：缺失的段落或键判为失败（fail closed），
绝不允许空集上的 ``all()`` 空真通过。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from production_restore_drill import (  # noqa: E402
    RECORD_SCHEMA_VERSION,
    REQUIRED_APP_SMOKE_CHECKS,
    REQUIRED_PII_ROUNDTRIP_CHECKS,
    STATUS_FAILED,
    STATUS_STARTED,
    build_drill_record,
    evaluate_drill_checks,
)


def _passing_inputs() -> dict:
    key_table_rows = {
        table: {"production": 3, "restored": 3}
        for table in ("patients", "encounters", "prescriptions")
    }
    return {
        "restored": {
            "alembic_revision": "abc123",
            "table_counts": {"patients": 3, "encounters": 3, "documents": 7},
        },
        "expected": {
            "alembic_revision": "abc123",
            "table_counts": {"patients": 3, "encounters": 3, "documents": 7},
        },
        "document_parity": {
            "backed_up_document_files": 7,
            "sha256_multiset_equal": True,
            "manifest_document_file_count": 7,
        },
        "key_table_rows": key_table_rows,
        "container_checks": {
            "app_smoke": {name: True for name in REQUIRED_APP_SMOKE_CHECKS},
            "isolated_pii_roundtrip": {name: True for name in REQUIRED_PII_ROUNDTRIP_CHECKS},
        },
    }


def test_all_checks_present_and_true_passes() -> None:
    checks = evaluate_drill_checks(**_passing_inputs())
    assert checks, "the enumerated check set must never be empty"
    assert all(checks.values())


def test_empty_app_smoke_section_fails_closed() -> None:
    inputs = _passing_inputs()
    inputs["container_checks"] = {
        "app_smoke": {},
        "isolated_pii_roundtrip": inputs["container_checks"]["isolated_pii_roundtrip"],
    }
    checks = evaluate_drill_checks(**inputs)
    assert not all(checks.values())
    assert not checks["app_smoke.health_ok"]
    assert not checks["app_smoke.login_rejects_wrong_password"]


def test_missing_container_sections_fail_closed() -> None:
    inputs = _passing_inputs()
    inputs["container_checks"] = {}
    checks = evaluate_drill_checks(**inputs)
    assert not all(checks.values())
    for name in REQUIRED_APP_SMOKE_CHECKS:
        assert not checks[f"app_smoke.{name}"]
    for name in REQUIRED_PII_ROUNDTRIP_CHECKS:
        assert not checks[f"isolated_pii_roundtrip.{name}"]


def test_non_boolean_truthy_values_fail_closed() -> None:
    inputs = _passing_inputs()
    inputs["container_checks"]["app_smoke"]["patients_authorized"] = "yes"
    inputs["container_checks"]["isolated_pii_roundtrip"]["hmac_exact_lookup_found"] = 1
    checks = evaluate_drill_checks(**inputs)
    assert checks["app_smoke.patients_authorized"] is False
    assert checks["isolated_pii_roundtrip.hmac_exact_lookup_found"] is False
    assert not all(checks.values())


def test_single_false_app_smoke_value_fails_whole_drill() -> None:
    inputs = _passing_inputs()
    inputs["container_checks"]["app_smoke"]["documents_anonymous_rejected"] = False
    checks = evaluate_drill_checks(**inputs)
    assert checks["app_smoke.documents_anonymous_rejected"] is False
    # Every other check can be fine; one failure must fail the drill.
    assert not all(checks.values())


def test_missing_pii_roundtrip_key_fails_closed() -> None:
    inputs = _passing_inputs()
    del inputs["container_checks"]["isolated_pii_roundtrip"]["stored_value_is_not_plaintext"]
    checks = evaluate_drill_checks(**inputs)
    assert checks["isolated_pii_roundtrip.stored_value_is_not_plaintext"] is False
    assert not all(checks.values())


def test_alembic_or_table_or_document_mismatch_fails() -> None:
    inputs = _passing_inputs()
    inputs["restored"]["alembic_revision"] = "older"
    assert not all(evaluate_drill_checks(**inputs).values())

    inputs = _passing_inputs()
    inputs["restored"]["table_counts"]["patients"] = 2
    assert not all(evaluate_drill_checks(**inputs).values())

    inputs = _passing_inputs()
    inputs["document_parity"]["sha256_multiset_equal"] = False
    assert not all(evaluate_drill_checks(**inputs).values())

    inputs = _passing_inputs()
    inputs["document_parity"]["manifest_document_file_count"] = 6
    assert not all(evaluate_drill_checks(**inputs).values())


def test_manifest_without_key_clinical_tables_fails_closed() -> None:
    inputs = _passing_inputs()
    inputs["key_table_rows"] = {}
    checks = evaluate_drill_checks(**inputs)
    assert checks["key_clinical_tables_present"] is False
    assert not all(checks.values())


def test_key_table_row_mismatch_fails_per_table() -> None:
    inputs = _passing_inputs()
    inputs["key_table_rows"]["patients"]["restored"] = 2
    checks = evaluate_drill_checks(**inputs)
    assert checks["key_table_rows.patients"] is False
    assert checks["key_table_rows.encounters"] is True
    assert not all(checks.values())


def test_drill_records_share_run_id_and_carry_controlled_codes_only() -> None:
    """一次演练的全部记录共享 run_id；失败记录只含受控代码，绝无自由文本。

    str(exc) 可能带出路径或敏感内容，所以记录里只有 stage（固定阶段名）
    和 error_type（异常类名）。
    """

    run_id = "abc123def456"
    started = build_drill_record(
        run_id=run_id,
        environment="production",
        status=STATUS_STARTED,
        stage="start",
        verified_at="2026-09-09T01:00:00+00:00",
        backup_id="bk-1",
    )
    phase = build_drill_record(
        run_id=run_id,
        environment="production",
        status=STATUS_STARTED,
        stage="restore_database",
        verified_at="2026-09-09T01:01:00+00:00",
        backup_id="bk-1",
    )
    failure = build_drill_record(
        run_id=run_id,
        environment="production",
        status=STATUS_FAILED,
        stage="restore_database",
        verified_at="2026-09-09T01:02:00+00:00",
        backup_id="bk-1",
        error_type="RuntimeError",
        isolated_database_removed=True,
    )
    for record in (started, phase, failure):
        assert record["run_id"] == run_id
        assert record["schema_version"] == RECORD_SCHEMA_VERSION
    assert started["stage"] == "start"
    assert phase["stage"] == "restore_database"
    assert failure["stage"] == "restore_database"
    assert failure["error_type"] == "RuntimeError"
    assert failure["isolated_database_removed"] is True
    # 受控代码：任何记录都不携带自由文本错误消息。
    for record in (started, phase, failure):
        assert "error" not in record
        assert set(record) <= {
            "schema_version",
            "verified_at",
            "run_id",
            "environment",
            "status",
            "stage",
            "backup_id",
            "error_type",
            "isolated_database_removed",
            "failed_checks",
            "manifest_sha256",
        }
