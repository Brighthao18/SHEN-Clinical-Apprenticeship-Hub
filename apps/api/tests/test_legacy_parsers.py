from __future__ import annotations

from pathlib import Path

from app.services.parsers.detector import (
    CURRENT_TEMPLATE,
    LEGACY_TEMPLATE,
    UNKNOWN_TEMPLATE,
    detect_template,
)
from app.services.parsers.jnu_legacy import JNULegacyParserError, JNULegacyPrescriptionParser

# Synthetic legacy slip modeled on the user-confirmed CASE005 layout family
# (prescription slip title, Rp marker, inline per-herb grams, N剂 usage).
# All identity values are fabricated; no real patient data is used.
LEGACY_SLIP_OCR = (
    """
配送

## 身份证:5100001950****1234 暨南大学附属第一医院 中药处方笺

类别:测试医保普通门诊

姓名:测试患者

诊卡号:*00000000*

性别:女

年龄:70岁

科别:中医科门诊

中医诊断:测试病证,气阴两虚

门诊号:700001

日期:2026年06月05日

Rp

金银花 $10\\mathrm{g}$ 连翘 $10\\mathrm{g}$ 桔梗 $5\\mathrm{g}$

浙贝母 $10\\mathrm{g}$ 黄芩(酒) $10\\mathrm{g}$ 北柴胡 $10\\mathrm{g}$

防风 10g
甘草(炙) 5g
板蓝根 15g

薄荷 5g 芦根 30g

7剂 用法: 口服

每日 1 剂, 水煎两次150毫升, 分 2 次, 饭后服, 温服

公费记账:0.00 元

付现:91.10 元

医师:测试医师(6012)

药品金额:91.10 元
""".strip()
    + "\n"
)

CURRENT_LAYOUT_FIXTURE = Path(__file__).parent / "fixtures" / "jnu_outpatient_v1.txt"


def test_legacy_slip_routes_to_legacy_template_current_layout_untouched() -> None:
    assert detect_template(LEGACY_SLIP_OCR).template == LEGACY_TEMPLATE
    current_text = CURRENT_LAYOUT_FIXTURE.read_text(encoding="utf-8")
    assert detect_template(current_text).template == CURRENT_TEMPLATE
    assert detect_template("random receipt\n总计 12 元\n").template == UNKNOWN_TEMPLATE


def test_legacy_parser_extracts_items_direct_gram_doses_and_ignores_pii() -> None:
    parsed = JNULegacyPrescriptionParser().parse(LEGACY_SLIP_OCR)
    assert parsed.template == LEGACY_TEMPLATE
    assert parsed.real_name == "测试患者"
    assert parsed.medical_record_number == "700001"
    assert parsed.encounter_date.isoformat() == "2026-06-05"
    assert parsed.total_decoctions == 7
    assert parsed.department == "中医科门诊"
    assert parsed.physician == "测试医师"
    assert {diagnosis.raw for diagnosis in parsed.diagnoses} == {"测试病证", "气阴两虚"}

    by_name = {item.raw_drug_name: item for item in parsed.prescription_items}
    assert len(parsed.prescription_items) == 11
    assert by_name["金银花"].explicit_package_dose_g is not None
    assert str(by_name["金银花"].explicit_package_dose_g) == "10"
    assert by_name["金银花"].package_count is None
    assert by_name["金银花"].is_direct_gram_dose is True
    assert str(by_name["金银花"].calculated_explicit_dose_g) == "10"
    assert by_name["黄芩"].processing_method == "酒"
    assert by_name["甘草"].processing_method == "炙"

    # Masked card numbers and administrative/PII lines are never captured.
    assert parsed.medical_record_number != "*00000000*"
    serialized = str(parsed.to_json())
    assert "身份证" not in serialized
    assert "5100001950" not in serialized


def test_legacy_parser_blocks_on_unconsumed_residue_instead_of_dropping_items() -> None:
    dirty = LEGACY_SLIP_OCR.replace(
        "防风 10g",
        "防风 10g 山茱萸 1",  # dose without unit: potential lost item
    )
    parsed = JNULegacyPrescriptionParser().parse(dirty)
    assert "LEGACY_UNPARSED_RESIDUAL" in parsed.warnings
    # The blocking decision is made by the validation policy, not the parser.
    from app.services.validation_policy import decide_ingestion

    assert decide_ingestion(parsed.warnings).decision == "unresolved"


def test_legacy_parser_fails_closed_below_threshold_or_without_date() -> None:
    try:
        JNULegacyPrescriptionParser().parse("中药处方笺\nRp\n没有任何字段")
    except JNULegacyParserError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("legacy parser accepted an unparseable slip")


# Variant A: printed slip with the package grammar, labeled 登记号 MRN,
# 付 counting, and an in-list decoction note (CASE023 family; synthetic).
PRINTED_PACKAGE_SLIP_OCR = (
    """
暨南大学附属第一医院中药处方笺

类别: 测试医保职工

证件号:4400001987****071X

姓名: 测试患者甲

登记号: 0002435765 科室: 中医科门诊 开方日期: 2026-07-10

临床诊断: 高血压

Rp:
(基)北柴胡(10g) 1包
(基)白茅根(5g) 6包
(基)姜厚朴(10g) 1包 (基)红景天 4袋
先煎
(基)石决明(15g) 2包

![](images/qr-code-noise-reference.jpg)

请微信扫码缴费

共 7 付(13味) 用法: 口服

每日1剂 每日2次 一次用量:150ml

药价: 单付:77.06元 合计:539.39元

医师:测试医师

核对/发药签名(签章):

打印:2026-07-10 10:54:48
""".strip()
    + "\n"
)

# Variant B/C: no Rp marker, direct-gram lines after the header block,
# OCR-noise footer lines (CASE031 family; synthetic).
RP_LESS_SLIP_OCR = (
    """
身份证:5100001950****1234

暨南大学附属第一医院 中药处方笺

类别:自费类型

医疗证/医保卡号:

姓名: 测试患者乙

性别:女

科别:中医科门诊

年龄:70岁

诊号:500101

医诊断:测试病证

日期:2026年04月17日

医诊断:气阴两虚

灯盏细辛 10g 苍耳子 10g 辛夷 10g

防风 10g 白术 15g 红芪 20g

川牛膝 10g 地肤子 15g 蛇床子 15g

红景天 12g 蝉蜕 5g 连翘 10g

7剂 用法: 口服

每日 1 剂, 水煎两次150毫升, 分 2 次, 饭后服, 温服

公费记账:0.00 元

医师:测试医师(6012)

药品金额:762.83 元

审 方:

核对:

调配:

外土口古桥  扣开日口中石
""".strip()
    + "\n"
)

# Table-wrapped variant: drug area survives only inside HTML table markup
# (CASE028 family; synthetic).
TABLE_WRAPPED_SLIP_OCR = (
    """
身份证:5100001950****5678

暨南大学附属第一医院 中药处方笺

类别:自费类型

医疗证/医保卡号:

<table>
<tr><td colspan="2">姓名: 测试患者丙</td><td>性别: 男</td></tr>
<tr><td colspan="3">就诊卡号:*01020304* 科别:中医科门诊 诊号:500101</td></tr>
<tr><td colspan="3">医诊断:测试病证二</td></tr>
<tr><td colspan="3">日期:2026年05月15日</td></tr>
<tr><td>Rp</td><td>金银花 10g</td><td>连翘 10g</td></tr>
<tr><td>桔梗 5g</td><td>黄芩 10g</td><td>甘草 5g</td></tr>
<tr><td colspan="3">6剂 用法: 口服</td></tr>
</table>

公费记账:0.00 元

医师:测试医师
""".strip()
    + "\n"
)


def test_printed_package_slip_uses_package_grammar_and_registration_mrn() -> None:
    parsed = JNULegacyPrescriptionParser().parse(PRINTED_PACKAGE_SLIP_OCR)
    assert parsed.real_name == "测试患者甲"
    assert parsed.medical_record_number == "0002435765"
    assert parsed.total_decoctions == 7  # 共 7 付, never 每日1剂
    assert parsed.encounter_date.isoformat() == "2026-07-10"
    by_name = {item.raw_drug_name: item for item in parsed.prescription_items}
    assert len(parsed.prescription_items) == 5
    assert str(by_name["北柴胡"].explicit_package_dose_g) == "10"
    assert str(by_name["北柴胡"].package_count) == "1"
    assert by_name["北柴胡"].original_package_unit == "包"
    assert by_name["北柴胡"].is_direct_gram_dose is False
    # The glued two-item line must split into both drugs with their own
    # dose/count, never a composite name.
    assert str(by_name["姜厚朴"].package_count) == "1"
    assert str(by_name["红景天"].package_count) == "4"
    assert by_name["红景天"].original_package_unit == "袋"
    assert by_name["红景天"].explicit_package_dose_g is None
    assert "LEGACY_UNPARSED_RESIDUAL" not in parsed.warnings
    assert "先煎" in (parsed.raw_instruction_text or "")


def test_rp_less_printed_slip_with_trailing_banner_and_glued_lines() -> None:
    # CASE024 family: no Rp marker, the 请微信 banner sits AFTER the drugs,
    # and MinerU glues package items pairwise. The zone must still capture
    # every item and reject composite names.
    synthetic = (
        """
门诊自取

暨南大学附属第一医院中药处方笺

类别:测试医保职工

证件号:4400001980****1234

姓名: 测试患者戊 煎药方式:自取登记号: 0002689534 科室: 中医科门诊 开方日期: 2026-07-10

临床诊断: 肝硬化

电话/地址:测试地址

(基)大腹皮(5g) 3包 (基)白术(10g) 1包

茯神(5g) 3包

(基)知母(10g) 1袋

![](images/qr-noise.jpg)

请微信扫码缴费

共 7 付(14味) 用法: 口服

药价: 单付:104.05元 合计:728.37元

医师: 测试医师
""".strip()
        + "\n"
    )
    parsed = JNULegacyPrescriptionParser().parse(synthetic)
    by_name = {item.raw_drug_name: item for item in parsed.prescription_items}
    assert set(by_name) == {"大腹皮", "白术", "茯神", "知母"}
    assert str(by_name["大腹皮"].package_count) == "3"
    assert str(by_name["白术"].explicit_package_dose_g) == "10"
    assert str(by_name["茯神"].explicit_package_dose_g) == "5"
    assert parsed.medical_record_number == "0002689534"
    assert parsed.total_decoctions == 7
    assert "LEGACY_UNPARSED_RESIDUAL" not in parsed.warnings


def test_rp_less_slip_zone_bounded_by_headers_and_footers() -> None:
    parsed = JNULegacyPrescriptionParser().parse(RP_LESS_SLIP_OCR)
    assert parsed.medical_record_number is None  # masked card + 诊号 rejected
    assert len(parsed.prescription_items) == 12
    assert parsed.total_decoctions == 7
    by_name = {item.raw_drug_name: item for item in parsed.prescription_items}
    assert str(by_name["红芪"].explicit_package_dose_g) == "20"
    assert by_name["蝉蜕"].is_direct_gram_dose is True
    # Trailing OCR stamp noise below the pharmacy footer stays out of the zone.
    assert "LEGACY_UNPARSED_RESIDUAL" not in parsed.warnings


def test_table_wrapped_slip_is_flattened_for_detection_and_items() -> None:
    from app.services.parsers.detector import LEGACY_TEMPLATE, detect_template

    assert detect_template(TABLE_WRAPPED_SLIP_OCR).template == LEGACY_TEMPLATE
    parsed = JNULegacyPrescriptionParser().parse(TABLE_WRAPPED_SLIP_OCR)
    assert parsed.real_name == "测试患者丙"
    assert {item.raw_drug_name for item in parsed.prescription_items} == {
        "金银花",
        "连翘",
        "桔梗",
        "黄芩",
        "甘草",
    }
    assert parsed.total_decoctions == 6
    assert parsed.medical_record_number is None  # masked card rejected
    assert "MEDICAL_RECORD_NUMBER_MISSING" in parsed.warnings


def test_zone_noise_lines_do_not_block_but_lost_drugs_still_do() -> None:
    # Real noise observed inside drug areas: QR image references, insurance
    # stamps, and per-item decoction-note prefixes on real drug lines.
    noisy = RP_LESS_SLIP_OCR.replace(
        "灯盏细辛 10g 苍耳子 10g 辛夷 10g",
        "![](images/deadbeefdeadbeefdeadbeefdeadbeef.jpg)\n"
        "慢性病用药\n"
        "后下 青蒿 10g\n"
        "灯盏细辛 10g 苍耳子 10g 辛夷 10g",
    )
    parsed = JNULegacyPrescriptionParser().parse(noisy)
    names = {item.raw_drug_name for item in parsed.prescription_items}
    assert "青蒿" in names
    assert "LEGACY_UNPARSED_RESIDUAL" not in parsed.warnings

    # An unconsumed line that could be a lost drug still fails closed.
    lost = RP_LESS_SLIP_OCR.replace("防风 10g", "防风 10g 山茱萸丸")
    parsed_lost = JNULegacyPrescriptionParser().parse(lost)
    assert "LEGACY_UNPARSED_RESIDUAL" in parsed_lost.warnings


def test_slip_title_routes_to_legacy_even_with_weak_markers() -> None:
    from app.services.parsers.detector import (
        CURRENT_TEMPLATE,
        LEGACY_TEMPLATE,
        detect_template,
    )

    # CASE025 family: title present, Rp/剂/金额 markers lost to OCR; marker
    # sums would lose to the current template without the title rule.
    weak = """
身份证:5100001950****0000

暨南大学附属第一医院 中药处方笺

类别:自费类型

E名:测试患者丁

性别:女

科别:中医科门诊

年龄:70岁

<table><tr><td>金银花 10g 连翘 10g</td></tr></table>
""".strip()
    assert detect_template(weak).template == LEGACY_TEMPLATE
    current_text = CURRENT_LAYOUT_FIXTURE.read_text(encoding="utf-8")
    assert detect_template(current_text).template == CURRENT_TEMPLATE
