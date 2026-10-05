"""JNU legacy herbal prescription slip parser (JNU_LEGACY_PRESCRIPTION_V1).

Independent of the current outpatient-record parser by design: the two
template families never share parsing hacks, and any layout that cannot be
confidently consumed stays unresolved instead of being forced through.

Observed layout variants (v0.2.0):

- handwritten-style slips (CASE005 family): ``Rp`` marker, several
  ``drug dose g`` pairs per line (per-decoction grams, no 包/袋), ``N剂``;
- printed slips (CASE023 family): ``Rp:`` marker, ``(基)药(10g) N包`` package
  grammar, labeled ``登记号`` MRN, ``共 N 付`` counting;
- table-wrapped slips whose drug area only survives MinerU as HTML table
  markup, and slips with no ``Rp`` marker at all (CASE031 family) — the drug
  zone is then bounded by header/footer labels instead.

Identity fields are often OCR-garbled or masked (身份证/地址/电话 are never
extracted or stored); masked card numbers and 诊号 visit serials are never
used as MRN.

The parser is deliberately conservative: unconsumed residue inside the
prescription zone raises ``LEGACY_UNPARSED_RESIDUAL`` so the document routes
to UNRESOLVED rather than silently dropping drug items.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation

from app.core.config import get_settings
from app.models.enums import DiagnosisType, Sex
from app.services.parsers.prescription import parse_prescription_line
from app.services.parsers.types import ParsedDiagnosis, ParsedDocument, ParsedPrescriptionItem

CANONICAL_HOSPITAL = "暨南大学附属第一医院（广州华侨医院）"

# Zone terminator lines after the drug block: usage/billing/fee/pharmacy rows.
_USAGE_TERMINATOR = re.compile(r"^(?:用法|服法|嘱|请|代煎|合剂|配送)")
# Billing/pharmacy footer rows end the drug zone entirely: stamp/noise lines
# below them (e.g. OCR garbage of signature blocks) must never be treated as
# prescription residue.
_FOOTER_TERMINATOR = re.compile(
    r"^(?:公费记账|付现|药品金额|药价|收费|金额|医师|医生|审\s*方|核对|核对发药|"
    r"调配|调\s*配|调剂|打印|处方当日|地址|电话|外配)"
)
# Header labels that bound the drug zone from above when no Rp marker exists.
# Only labeled fields and bare registration numbers belong here: generic
# noise (markdown/image-reference lines, the 请微信 banner) appears at
# varying positions — sometimes after the drugs — and would push the Rp-less
# zone start past the whole prescription. In-zone noise is skipped by the
# zone scanner itself.
_HEADER_LINE = re.compile(
    r"^(?:配送|门诊自取|类别|证件号|身份证|医疗证|医保卡号|"
    r"姓名|姓\s*名|E名|名\s*[:：]|诊卡号|就诊卡号|性别|年龄|科别|科室|诊号|登记号|"
    r"开方日期|日期|医诊断|中医诊断|西医诊断|临床诊断|地址|电话|目医诊断)"
    r"|^\d{6,14}$"
)
_DRUG_DOSE_PATTERN = re.compile(
    r"(?:(?:[（(]\s*基\s*[）)])\s*)?"
    r"(?P<name>[\u4e00-\u9fff·]{1,20}(?:[（(][^（）()]{1,12}[）)])?)"
    r"\s*(?P<dose>\d+(?:\.\d+)?)\s*(?:g|克)\b",
    re.IGNORECASE,
)
_TOTAL_DECOCTIONS_LINE = re.compile(r"^(?:共\s*)?(?P<count>\d{1,3})\s*[剂付]")
_TOTAL_DECOCTIONS_ANY = re.compile(r"共\s*(?P<count>\d{1,3})\s*[剂付]")
_TOTAL_DECOCTIONS_FALLBACK = re.compile(r"(?<!每日)(?<!每剂)(?P<count>\d{1,3})\s*[剂付]")
_BASIC_PREFIX = re.compile(r"^[（(]\s*基\s*[）)]\s*")
_PROCESSING_SUFFIX = re.compile(r"[（(](?P<method>[^（）()0-9]{1,12})[）)]\s*$")
# Inline markdown image references (QR/barcode areas) are extraction noise.
_IMAGE_REFERENCE = re.compile(r"^!\[.*\]\(.*\)$")
# Per-item decoction notes printed on the same line as the drug (后下 青蒿 10g)
# or alone between items; legitimate instructions, never lost drugs.
_DECOCTION_NOTE = re.compile(r"先煎|后下|烊化|冲服|包煎|另煎|兑服|捣碎|布包")
# MinerU glues consecutive package items into one line; each item ends at its
# 包/袋 count.
_PACKAGE_ITEM_SPLIT = re.compile(r"(?<=[包袋])\s+")
# Bounded administrative stamps observed inside real drug areas; anything
# else unconsumed still fails closed as residue.
_ADMIN_LABEL = re.compile(r"^(?:慢性病用药|门特用药|门慢用药|特殊病种用药|大额用药|普通门诊用药)$")


def flatten_mineru_markup(text: str) -> str:
    """Flatten MinerU HTML-table/br markup into plain lines.

    Shared detection-level preprocessing only; each parser keeps its own
    field grammar. Without this, table-wrapped drug areas glue into one line
    and both detection and zone parsing fail.
    """

    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</\s*(?:td|th|tr)\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*(?:td|th|tr)\b[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?\s*table\b[^>]*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return text


class JNULegacyParserError(ValueError):
    pass


class JNULegacyPrescriptionParser:
    template = "JNU_LEGACY_PRESCRIPTION_V1"

    def __init__(self) -> None:
        self.version = get_settings().jnu_legacy_parser_version

    @staticmethod
    def detect_score(text: str) -> float:
        normalized = flatten_mineru_markup(unicodedata.normalize("NFKC", text))
        markers = [
            ("处方笺" in normalized, 0.45),
            (
                any(
                    re.match(r"^R\s*P\s*[:：]?\s*$", line.strip(), re.IGNORECASE)
                    for line in normalized.splitlines()
                ),
                0.25,
            ),
            ("科别" in normalized, 0.10),
            (
                "用法" in normalized
                and (
                    bool(_TOTAL_DECOCTIONS_ANY.search(normalized))
                    or any(
                        _TOTAL_DECOCTIONS_LINE.match(line.strip())
                        for line in normalized.splitlines()
                    )
                ),
                0.10,
            ),
            (
                any(marker in normalized for marker in ("公费记账", "付现", "药品金额", "药价")),
                0.10,
            ),
        ]
        return min(1.0, sum(weight for present, weight in markers if present))

    def parse(self, text: str) -> ParsedDocument:
        normalized = self._normalize(text)
        if self.detect_score(normalized) < 0.5:
            raise JNULegacyParserError("Document does not meet the legacy prescription threshold")

        encounter_date = self._parse_date(normalized)
        if encounter_date is None:
            raise JNULegacyParserError("Encounter date is missing or invalid")

        real_name = self._text_field(
            normalized,
            r"(?:姓\s*名|姓名|名)\s*[:：]\s*([\u4e00-\u9fff·]{2,20})(?=\s|$|性别|年龄|科别)",
        )
        sex = self._parse_sex(normalized)
        age = self._int_field(normalized, r"年龄\s*[:：]?\s*(\d{1,3})\s*岁?")
        medical_record_number = self._parse_mrn(normalized)
        department = self._text_field(normalized, r"科别\s*[:：]?\s*([^\n]{1,40}?)(?=\n|$)")
        physician = self._text_field(
            normalized, r"(?:医师|医生)\s*[:：]\s*([\u4e00-\u9fff·]{1,20})"
        )
        diagnoses = self._parse_diagnoses(normalized)

        zone_lines, instruction_lines, residual_lines = self._prescription_zone(normalized)
        parsed_items: list[ParsedPrescriptionItem] = []
        for line in zone_lines:
            parsed_items.extend(self._parse_drug_line(line))
        total_decoctions = self._total_decoctions(normalized, instruction_lines)

        warnings: list[str] = []
        if real_name is None:
            warnings.append("PATIENT_NAME_MISSING")
        if medical_record_number is None:
            warnings.append("MEDICAL_RECORD_NUMBER_MISSING")
        if sex == Sex.UNKNOWN:
            warnings.append("SEX_UNKNOWN")
        if not parsed_items:
            warnings.append("PRESCRIPTION_NOT_FOUND")
        if residual_lines:
            warnings.append("LEGACY_UNPARSED_RESIDUAL")

        return ParsedDocument(
            template=self.template,
            parser_version=self.version,
            hospital=CANONICAL_HOSPITAL,
            real_name=real_name,
            sex=sex,
            age=age,
            encounter_date=encounter_date,
            medical_record_number=medical_record_number,
            department=department,
            physician=physician,
            chief_complaint=None,
            present_illness=None,
            past_history=None,
            allergy=None,
            vital_signs=None,
            advice=None,
            treatment_record=None,
            diagnoses=diagnoses,
            prescription_items=parsed_items,
            total_decoctions=total_decoctions,
            raw_instruction_text="\n".join([*instruction_lines, *residual_lines]).strip() or None,
            warnings=warnings,
        )

    @staticmethod
    def _normalize(text: str) -> str:
        text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
        text = flatten_mineru_markup(text)
        text = re.sub(
            r"\$?\s*(\d+(?:\.\d+)?)\s*\\mathrm\{g\}\s*\$?",
            r"\1g",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\\\s*", "", text)
        lines: list[str] = []
        for raw_line in text.splitlines():
            line = re.sub(r"^\s*(?:#{1,6}|[-*•>|])\s*", "", raw_line).strip()
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _parse_drug_line(line: str) -> list[ParsedPrescriptionItem]:
        """Consume one drug line with either observed legacy grammar.

        Handwritten-style slips print ``drug 10g`` pairs (per-decoction
        grams); printed slips use the ``(基)drug(10g) N包`` package grammar.
        MinerU sometimes glues several package items into one line, so the
        package path splits on 包/袋 boundaries and accepts the line only if
        every segment parses; a composite name containing digits means the
        split failed and the line must fail closed as residue instead of
        producing a fabricated item.
        """

        items: list[ParsedPrescriptionItem] = []
        for match in _DRUG_DOSE_PATTERN.finditer(line):
            working_name = match.group("name").strip()
            is_basic = False
            basic_match = _BASIC_PREFIX.match(working_name)
            if basic_match:
                is_basic = True
                working_name = working_name[basic_match.end() :].strip()
            processing_method: str | None = None
            processing_match = _PROCESSING_SUFFIX.search(working_name)
            if processing_match:
                processing_method = processing_match.group("method").strip()
                working_name = working_name[: processing_match.start()].strip()
            if not working_name:
                continue
            try:
                dose = Decimal(match.group("dose"))
            except InvalidOperation:
                continue
            items.append(
                ParsedPrescriptionItem(
                    original_text=match.group(0).strip(),
                    is_basic_drug=is_basic,
                    raw_drug_name=working_name,
                    processing_method=processing_method,
                    explicit_package_dose_g=dose,
                    package_count=None,
                    original_package_unit=None,
                    normalized_package_unit=None,
                    is_direct_gram_dose=True,
                )
            )
        if items:
            return items
        segments = _PACKAGE_ITEM_SPLIT.split(line.strip())
        packaged: list[ParsedPrescriptionItem] = []
        for segment in segments:
            candidate = parse_prescription_line(segment, allow_missing_package_unit=True)
            if candidate is None or re.search(r"\d", candidate.raw_drug_name):
                return []
            packaged.append(candidate)
        return packaged

    @staticmethod
    def _prescription_zone(
        text: str,
    ) -> tuple[list[str], list[str], list[str]]:
        """Split the drug block into drug lines, instruction lines, residue.

        The zone starts after an ``Rp``/``Rp:`` marker when present; otherwise
        (CASE031 family) it starts after the last header-label line. The zone
        ends at the first usage/billing/pharmacy footer line. Residue is any
        in-zone line with CJK or digits that neither grammar consumes; it
        blocks auto-accept because it may be a lost drug item.
        """

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        rp_indices = [
            index
            for index, line in enumerate(lines)
            if re.match(r"^R\s*P\s*[:：]?\s*$", line, re.IGNORECASE)
        ]
        if rp_indices:
            zone_start = rp_indices[-1] + 1
        else:
            last_header = -1
            for index, line in enumerate(lines):
                if _HEADER_LINE.match(line):
                    last_header = index
            zone_start = last_header + 1

        drug_lines: list[str] = []
        instruction_lines: list[str] = []
        residual_lines: list[str] = []
        for line in lines[zone_start:]:
            if _FOOTER_TERMINATOR.match(line):
                instruction_lines.append(line)
                break
            if _IMAGE_REFERENCE.match(line) or _ADMIN_LABEL.match(line):
                continue
            if _USAGE_TERMINATOR.match(line):
                instruction_lines.append(line)
                continue
            if _TOTAL_DECOCTIONS_LINE.match(line):
                instruction_lines.append(line)
                continue
            items = JNULegacyPrescriptionParser._parse_drug_line(line)
            if items:
                drug_lines.append(line)
                remainder = _DRUG_DOSE_PATTERN.sub("", line)
                for item in items:
                    if item.original_text:
                        remainder = remainder.replace(item.original_text, "", 1)
                remainder = _DECOCTION_NOTE.sub("", remainder)
                if re.search(
                    r"[\u4e00-\u9fff]|(?<![\d.])\d+(?![\d.])", remainder.strip(" ，,、;；")
                ):
                    residual_lines.append(line)
            elif re.search(r"[\u4e00-\u9fff0-9]", line):
                # Usage/dosage prose (剂/服/煎/毫升/煎法注记) never carries a
                # gram-dose pair; anything else unconsumed is a potentially
                # lost item.
                if re.search(r"剂|服|煎|毫升|ml", line, re.IGNORECASE) or _DECOCTION_NOTE.search(
                    line
                ):
                    instruction_lines.append(line)
                else:
                    residual_lines.append(line)
        return drug_lines, instruction_lines, residual_lines

    @staticmethod
    def _total_decoctions(text: str, instruction_lines: list[str]) -> int | None:
        for line in instruction_lines:
            match = _TOTAL_DECOCTIONS_LINE.match(line.strip())
            if match:
                return int(match.group("count"))
        match = _TOTAL_DECOCTIONS_ANY.search(text)
        if match:
            return int(match.group("count"))
        compact = re.sub(r"\s+", "", text)
        match = _TOTAL_DECOCTIONS_FALLBACK.search(compact)
        return int(match.group("count")) if match else None

    @staticmethod
    def _parse_mrn(text: str) -> str | None:
        # 诊号 is a per-visit serial number, not a stable patient fingerprint
        # (confirmed against user-confirmed Gold on the CASE005 family and by
        # two different patients sharing 诊号 500101 in the expansion set), so
        # it must never feed exact patient matching.
        for match in re.finditer(
            r"(?:病历号|门诊号|病案号|登记号|卡号)\s*[:：]?\s*([A-Za-z0-9_-]{2,40})", text
        ):
            candidate = match.group(1).strip()
            # Masked values (e.g. 诊卡号:*04773423*) are unusable as identity.
            if "*" not in candidate:
                return candidate
        return None

    @staticmethod
    def _parse_diagnoses(text: str) -> list[ParsedDiagnosis]:
        diagnoses: list[ParsedDiagnosis] = []
        for match in re.finditer(
            r"(?P<label>中医诊断|西医诊断|医诊断|诊断)\s*[:：]\s*(?P<value>[^\n]{1,120})", text
        ):
            label = match.group("label")
            diagnosis_type = DiagnosisType.TCM_DISEASE if "中医" in label else DiagnosisType.WESTERN
            for fragment in re.split(r"[，,、;；]", match.group("value")):
                raw = fragment.strip()
                if not raw or not re.search(r"[\u4e00-\u9fff]", raw):
                    continue
                diagnoses.append(
                    ParsedDiagnosis(
                        raw=raw,
                        normalized=raw,
                        diagnosis_type=diagnosis_type,
                        is_primary=False,
                    )
                )
        return diagnoses

    @staticmethod
    def _parse_date(text: str) -> date | None:
        match = re.search(
            r"(?:日期|就诊日期|开方日期)\s*[:：]?\s*(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?",
            text,
        )
        if not match:
            return None
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None

    @staticmethod
    def _parse_sex(text: str) -> Sex:
        value = JNULegacyPrescriptionParser._text_field(text, r"性别\s*[:：]?\s*([男女])")
        if value == "男":
            return Sex.MALE
        if value == "女":
            return Sex.FEMALE
        return Sex.UNKNOWN

    @staticmethod
    def _text_field(text: str, pattern: str) -> str | None:
        match = re.search(pattern, text, flags=re.MULTILINE)
        return match.group(1).strip() if match else None

    @staticmethod
    def _int_field(text: str, pattern: str) -> int | None:
        value = JNULegacyPrescriptionParser._text_field(text, pattern)
        return int(value) if value is not None else None
