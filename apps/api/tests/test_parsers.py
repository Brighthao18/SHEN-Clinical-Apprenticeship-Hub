from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.models.enums import DiagnosisType, Sex, SymptomTrend
from app.services.drugs import calculate_course_total_dose
from app.services.parsers.jnu import JNUHospitalOutpatientParser
from app.services.parsers.prescription import parse_prescription_line

FIXTURE = Path(__file__).parent / "fixtures" / "jnu_outpatient_v1.txt"


def test_prescription_parser_handles_basic_processing_and_package_arithmetic() -> None:
    item = parse_prescription_line("（基）枳壳（麸炒）（10g）1包")
    assert item is not None
    assert item.is_basic_drug is True
    assert item.raw_drug_name == "枳壳"
    assert item.processing_method == "麸炒"
    assert item.explicit_package_dose_g == Decimal("10")
    assert item.package_count == Decimal("1")
    assert item.original_package_unit == "包"
    assert item.normalized_package_unit == "package"
    assert item.calculated_explicit_dose_g == Decimal("10")


def test_confirmed_jnu_package_dose_is_per_decoction_and_course_total_is_derived() -> None:
    item = parse_prescription_line("某药（10g）2包")
    assert item is not None
    assert item.explicit_package_dose_g == Decimal("10")
    assert item.package_count == Decimal("2")
    assert item.calculated_explicit_dose_g == Decimal("20")
    assert calculate_course_total_dose(item.calculated_explicit_dose_g, 7) == Decimal("140")


def test_prescription_parser_keeps_missing_grams_null() -> None:
    item = parse_prescription_line("红景天2袋")
    assert item is not None
    assert item.raw_drug_name == "红景天"
    assert item.package_count == Decimal("2")
    assert item.explicit_package_dose_g is None
    assert item.calculated_explicit_dose_g is None


def test_missing_package_unit_is_only_a_jnu_review_candidate() -> None:
    assert parse_prescription_line("测试药（10g）2") is None

    item = parse_prescription_line(
        "测试药（10g）2",
        allow_missing_package_unit=True,
    )
    assert item is not None
    assert item.explicit_package_dose_g == Decimal("10")
    assert item.package_count == Decimal("2")
    assert item.original_package_unit is None
    assert item.normalized_package_unit is None
    assert item.calculated_explicit_dose_g == Decimal("20")

    assert (
        parse_prescription_line(
            "测试药2",
            allow_missing_package_unit=True,
        )
        is None
    )


def test_jnu_parser_extracts_clinical_fields_without_filling_empty_history() -> None:
    parsed = JNUHospitalOutpatientParser().parse(FIXTURE.read_text(encoding="utf-8"))
    assert parsed.template == "JNU_HOSPITAL_OUTPATIENT_V1"
    assert parsed.real_name == "张三"
    assert parsed.sex == Sex.MALE
    assert parsed.age == 70
    assert parsed.encounter_date.isoformat() == "2026-08-14"
    assert parsed.medical_record_number == "JNU-000128"
    assert parsed.department == "中医科门诊"
    assert parsed.present_illness is None
    assert parsed.past_history == "胰十二指肠切除术"
    assert len(parsed.prescription_items) == 11
    assert parsed.total_decoctions == 7
    assert "一次150ml" in (parsed.raw_instruction_text or "")
    sleep = next(item for item in parsed.symptoms if item.normalized == "睡眠异常")
    assert sleep.trend == SymptomTrend.IMPROVED
    assert parsed.diagnoses[0].diagnosis_type == DiagnosisType.PROCEDURE_STATUS
    assert parsed.diagnoses[0].is_primary is True


def test_jnu_parser_flattens_mineru_html_cells_and_flags_positional_mrn() -> None:
    synthetic_ocr = """
# 暨南大学附属第一医院
广州华侨医院
门（急）诊病历
1234567890
姓名：测试甲 性别：女 年龄：40岁 日期：2026-08-20 科室：中医科门诊
主诉：测试主诉
诊断：测试诊断（主诊断）
医嘱：<table><tr><td>（基）测试药甲（10g）2包</td>
<td>测试药乙2袋 （基）测试药丙（5g）1包</td></tr>
<tr><td colspan="2">共7剂 每日1剂</td></tr></table>
医师：测试医师
"""

    parsed = JNUHospitalOutpatientParser().parse(synthetic_ocr)

    assert parsed.medical_record_number == "1234567890"
    assert "UNLABELED_HEADER_MRN_INFERRED" in parsed.warnings
    assert [item.raw_drug_name for item in parsed.prescription_items] == [
        "测试药甲",
        "测试药乙",
        "测试药丙",
    ]
    assert parsed.prescription_items[0].calculated_explicit_dose_g == Decimal("20")
    assert parsed.prescription_items[1].explicit_package_dose_g is None


def test_jnu_parser_merges_split_dose_and_flags_missing_package_unit() -> None:
    synthetic_ocr = """
# 暨南大学附属第一医院
广州华侨医院
门（急）诊病历
1234567890
姓名：测试甲 性别：女 年龄：40岁 日期：2026-08-20 科室：中医科门诊
主诉：测试主诉
诊断：测试诊断（主诊断）
医嘱：
（基）测试药甲（10g）2
（基）测试药乙
（5g）3包
（基）未完整药（麸）
共7剂
医师：测试医师
"""

    parsed = JNUHospitalOutpatientParser().parse(synthetic_ocr)

    assert [item.raw_drug_name for item in parsed.prescription_items] == [
        "测试药甲",
        "测试药乙",
    ]
    assert parsed.prescription_items[0].package_count == Decimal("2")
    assert parsed.prescription_items[0].original_package_unit is None
    assert parsed.prescription_items[1].calculated_explicit_dose_g == Decimal("15")
    assert "PACKAGE_UNIT_MISSING_IN_OCR" in parsed.warnings
    assert "未完整药" in (parsed.raw_instruction_text or "")
