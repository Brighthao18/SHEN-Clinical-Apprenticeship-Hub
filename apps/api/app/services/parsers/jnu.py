from __future__ import annotations

import html
import re
import unicodedata
from datetime import date

from app.core.config import get_settings
from app.models.enums import DiagnosisType, Sex, SymptomStatus, SymptomTrend
from app.services.parsers.prescription import parse_prescription_line
from app.services.parsers.types import ParsedDiagnosis, ParsedDocument, ParsedSymptom

CANONICAL_HOSPITAL = "暨南大学附属第一医院（广州华侨医院）"

SECTION_LABELS: dict[str, tuple[str, ...]] = {
    "chief_complaint": ("主诉",),
    "present_illness": ("现病史",),
    "past_history": ("既往史",),
    "allergy": ("过敏史", "药物过敏史"),
    "vital_signs": ("生命体征", "体格检查"),
    "diagnosis": ("诊断", "初步诊断"),
    "advice": ("医嘱",),
    "treatment_record": ("处理记录", "处理"),
    "prescription": ("处方", "中药处方", "方药"),
    "physician": ("医师", "医生", "医师签名"),
}


class JNUParserError(ValueError):
    pass


class JNUHospitalOutpatientParser:
    template = "JNU_HOSPITAL_OUTPATIENT_V1"

    def __init__(self) -> None:
        self.version = get_settings().jnu_parser_version

    @staticmethod
    def detect_score(text: str) -> float:
        normalized = unicodedata.normalize("NFKC", text)
        markers = [
            ("暨南大学附属第一医院" in normalized or "广州华侨医院" in normalized, 0.45),
            ("门(急)诊病历" in normalized or "门急诊病历" in normalized, 0.25),
            ("主诉" in normalized, 0.08),
            ("诊断" in normalized, 0.08),
            ("性别" in normalized and "年龄" in normalized, 0.07),
            ("医师" in normalized or "医生" in normalized, 0.07),
        ]
        return min(1.0, sum(weight for present, weight in markers if present))

    def parse(self, text: str) -> ParsedDocument:
        normalized = self._normalize(text)
        if self.detect_score(normalized) < 0.55:
            raise JNUParserError("Document does not meet the JNU template threshold")

        sections = self._sections(normalized)
        encounter_date = self._parse_date(normalized)
        if encounter_date is None:
            raise JNUParserError("Encounter date is missing or invalid")

        name_matches = re.findall(
            r"姓名\s*[:：]?\s*([\u4e00-\u9fff·]{1,20})(?=\s|$|性别|年龄|日期)",
            normalized,
        )
        real_name = name_matches[0] if name_matches else None
        sex = self._parse_sex(normalized)
        age = self._int_field(normalized, r"年龄\s*[:：]?\s*(\d{1,3})\s*岁?")
        labeled_mrn = self._text_field(
            normalized, r"(?:病历号|门诊号|病案号)\s*[:：]?\s*([A-Za-z0-9_-]{2,40})"
        )
        mrn = labeled_mrn or self._unlabeled_header_mrn(normalized)
        department = self._text_field(
            normalized,
            r"科室\s*[:：]?\s*([^\n\r]{1,40}?)(?=\s+(?:医师|医生|姓名|性别|年龄|日期)\s*[:：]|$|\n)",
        )
        physician = sections.get("physician") or self._text_field(
            normalized, r"(?:医师|医生|医师签名)\s*[:：]?\s*([\u4e00-\u9fff·]{1,20})"
        )

        chief = sections.get("chief_complaint")
        symptoms = self._parse_symptoms(chief)
        diagnoses = self._parse_diagnoses(sections.get("diagnosis"))

        prescription_text = sections.get("prescription") or ""
        prescription_lines = self._merge_split_prescription_lines(
            [line.strip() for line in prescription_text.splitlines() if line.strip()]
        )
        parsed_items = []
        instruction_lines: list[str] = []
        for line in prescription_lines:
            item = parse_prescription_line(line, allow_missing_package_unit=True)
            if item:
                parsed_items.append(item)
            else:
                instruction_lines.append(line)

        if not parsed_items:
            fallback_lines = self._merge_split_prescription_lines(
                [line.strip() for line in normalized.splitlines() if line.strip()]
            )
            for line in fallback_lines:
                item = parse_prescription_line(line, allow_missing_package_unit=True)
                if item:
                    parsed_items.append(item)
                elif self._looks_like_prescription_fragment(line):
                    instruction_lines.append(line)

        total_decoctions = self._int_field(normalized, r"共\s*(\d{1,3})\s*剂")
        total_line = f"共{total_decoctions}剂" if total_decoctions is not None else None
        if total_line and not any("共" in line and "剂" in line for line in instruction_lines):
            instruction_lines.insert(0, total_line)

        warnings: list[str] = []
        if real_name is None:
            warnings.append("PATIENT_NAME_MISSING")
        if mrn is None:
            warnings.append("MEDICAL_RECORD_NUMBER_MISSING")
        elif labeled_mrn is None:
            warnings.append("UNLABELED_HEADER_MRN_INFERRED")
        if sex == Sex.UNKNOWN:
            warnings.append("SEX_UNKNOWN")
        if len(set(name_matches)) > 1:
            warnings.append("MULTIPLE_PATIENT_NAMES_SUSPECTED")
        if not parsed_items:
            warnings.append("PRESCRIPTION_NOT_FOUND")
        if any(
            item.package_count is not None and item.original_package_unit is None
            for item in parsed_items
        ):
            warnings.append("PACKAGE_UNIT_MISSING_IN_OCR")

        return ParsedDocument(
            template=self.template,
            parser_version=self.version,
            hospital=CANONICAL_HOSPITAL,
            real_name=real_name,
            sex=sex,
            age=age,
            encounter_date=encounter_date,
            medical_record_number=mrn,
            department=department,
            physician=self._clean_single_line(physician),
            chief_complaint=chief,
            present_illness=sections.get("present_illness"),
            past_history=sections.get("past_history"),
            allergy=sections.get("allergy"),
            vital_signs=sections.get("vital_signs"),
            advice=sections.get("advice"),
            treatment_record=sections.get("treatment_record"),
            symptoms=symptoms,
            diagnoses=diagnoses,
            prescription_items=parsed_items,
            total_decoctions=total_decoctions,
            raw_instruction_text="\n".join(instruction_lines).strip() or None,
            warnings=warnings,
        )

    @staticmethod
    def _normalize(text: str) -> str:
        text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
        text = html.unescape(text)
        text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</\s*(?:td|th|tr)\s*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<\s*(?:td|th|tr)\b[^>]*>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"</?\s*table\b[^>]*>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(
            r"\$?\s*(\d+(?:\.\d+)?)\s*\\mathrm\{g\}\s*\$?",
            r"\1g",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(
            r"(?<=[包袋])\s+(?=(?:[（(]\s*基\s*[）)]\s*)?[\u4e00-\u9fff])",
            "\n",
            text,
        )
        lines: list[str] = []
        for raw_line in text.splitlines():
            line = re.sub(r"^\s*(?:#{1,6}|[-*•>|])\s*", "", raw_line).strip()
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _unlabeled_header_mrn(text: str) -> str | None:
        header = text.split("姓名", 1)[0]
        candidates = set(re.findall(r"(?m)^\s*(\d{8,12})\s*$", header))
        return next(iter(candidates)) if len(candidates) == 1 else None

    @staticmethod
    def _merge_split_prescription_lines(lines: list[str]) -> list[str]:
        merged: list[str] = []
        index = 0
        dose_only = re.compile(
            r"^[（(]\s*\d+(?:\.\d+)?\s*(?:g|克)\s*[）)]\s*"
            r"\d+(?:\.\d+)?\s*(?:包|袋)?\s*$",
            re.IGNORECASE,
        )
        while index < len(lines):
            current = lines[index].strip()
            if (
                index + 1 < len(lines)
                and re.search(r"[\u4e00-\u9fff]", current)
                and not re.search(r"\d+\s*(?:包|袋)\s*$", current)
                and dose_only.fullmatch(lines[index + 1].strip())
            ):
                merged.append(f"{current}{lines[index + 1].strip()}")
                index += 2
                continue
            merged.append(current)
            index += 1
        return merged

    @staticmethod
    def _looks_like_prescription_fragment(line: str) -> bool:
        return bool(
            re.search(r"^[（(]\s*基\s*[）)]", line)
            or re.search(r"[（(]\s*\d+(?:\.\d+)?\s*(?:g|克)\s*[）)]", line)
            or re.search(r"\d+(?:\.\d+)?\s*(?:包|袋)\s*$", line)
        )

    @staticmethod
    def _sections(text: str) -> dict[str, str | None]:
        alias_to_key = {alias: key for key, aliases in SECTION_LABELS.items() for alias in aliases}
        aliases = sorted(alias_to_key, key=len, reverse=True)
        label_pattern = re.compile(
            rf"^\s*(?P<label>{'|'.join(map(re.escape, aliases))})\s*[:：]?\s*(?P<value>.*)$"
        )
        collected: dict[str, list[str]] = {key: [] for key in SECTION_LABELS}
        current: str | None = None
        for line in text.splitlines():
            match = label_pattern.match(line)
            if match:
                current = alias_to_key[match.group("label")]
                value = match.group("value").strip()
                if value:
                    collected[current].append(value)
                continue
            if current is not None and line.strip():
                collected[current].append(line.strip())

        result: dict[str, str | None] = {}
        for key, values in collected.items():
            joined = "\n".join(values).strip()
            result[key] = joined or None
        return result

    @staticmethod
    def _parse_date(text: str) -> date | None:
        match = re.search(
            r"(?:日期|就诊日期)\s*[:：]?\s*(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?",
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
        value = JNUHospitalOutpatientParser._text_field(text, r"性别\s*[:：]?\s*([男女])")
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
        value = JNUHospitalOutpatientParser._text_field(text, pattern)
        return int(value) if value is not None else None

    @staticmethod
    def _clean_single_line(value: str | None) -> str | None:
        if not value:
            return None
        return value.splitlines()[0].strip() or None

    @staticmethod
    def _parse_symptoms(chief: str | None) -> list[ParsedSymptom]:
        if not chief:
            return []
        symptoms: list[ParsedSymptom] = []
        for fragment in re.split(r"[，,、；;。\n]+", chief):
            raw = fragment.strip()
            if not raw:
                continue
            status = SymptomStatus.PRESENT
            trend: SymptomTrend | None = None
            normalized = raw
            if raw.startswith("无") and len(raw) > 1:
                status = SymptomStatus.ABSENT
                normalized = raw[1:]
            if any(word in raw for word in ("改善", "好转", "减轻")):
                trend = SymptomTrend.IMPROVED
            elif any(word in raw for word in ("加重", "恶化")):
                trend = SymptomTrend.WORSENED
            elif any(word in raw for word in ("消失", "已愈")):
                trend = SymptomTrend.RESOLVED
                status = SymptomStatus.ABSENT
            if "睡眠" in raw:
                normalized = "睡眠异常"
            else:
                normalized = re.sub(
                    r"(?:有)?(?:改善|好转|减轻|加重|恶化|消失|已愈)$", "", normalized
                )
                normalized = normalized.strip() or raw
            symptoms.append(
                ParsedSymptom(raw=raw, normalized=normalized, status=status, trend=trend)
            )
        return symptoms

    @staticmethod
    def _parse_diagnoses(value: str | None) -> list[ParsedDiagnosis]:
        if not value:
            return []
        diagnoses: list[ParsedDiagnosis] = []
        for fragment in re.split(r"[；;\n]+", value):
            raw = fragment.strip()
            if not raw:
                continue
            is_primary = bool(re.search(r"[（(]\s*主诊断\s*[）)]", raw))
            normalized = re.sub(r"[（(]\s*主诊断\s*[）)]", "", raw).strip()
            diagnosis_type = (
                DiagnosisType.PROCEDURE_STATUS if "术后" in normalized else DiagnosisType.WESTERN
            )
            diagnoses.append(
                ParsedDiagnosis(
                    raw=raw,
                    normalized=normalized or None,
                    diagnosis_type=diagnosis_type,
                    is_primary=is_primary,
                )
            )
        return diagnoses
