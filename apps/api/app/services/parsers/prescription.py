from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation

from app.services.parsers.types import ParsedPrescriptionItem

BASIC_PATTERN = re.compile(r"^[（(]\s*基\s*[）)]\s*")
PACKAGE_PATTERN = re.compile(r"(?P<count>\d+(?:\.\d+)?)\s*(?P<unit>包|袋)\s*$", re.I)
MISSING_UNIT_COUNT_PATTERN = re.compile(r"(?P<count>\d+(?:\.\d+)?)\s*$")
DOSE_PATTERN = re.compile(r"[（(]\s*(?P<dose>\d+(?:\.\d+)?)\s*(?:g|克)\s*[）)]\s*$", re.I)
PAREN_PATTERN = re.compile(r"[（(]\s*(?P<value>[^（）()]+?)\s*[）)]\s*$")


def parse_prescription_line(
    line: str,
    *,
    allow_missing_package_unit: bool = False,
) -> ParsedPrescriptionItem | None:
    original = unicodedata.normalize("NFKC", line).strip()
    if not original:
        return None

    working = original
    basic_match = BASIC_PATTERN.match(working)
    is_basic = basic_match is not None
    if basic_match:
        working = working[basic_match.end() :].strip()

    package_count: Decimal | None = None
    original_unit: str | None = None
    normalized_unit: str | None = None
    missing_package_unit = False
    package_match = PACKAGE_PATTERN.search(working)
    if package_match:
        try:
            package_count = Decimal(package_match.group("count"))
        except InvalidOperation:
            return None
        original_unit = package_match.group("unit")
        normalized_unit = "package"
        working = working[: package_match.start()].strip()
    else:
        missing_unit_match = (
            MISSING_UNIT_COUNT_PATTERN.search(working) if allow_missing_package_unit else None
        )
        if missing_unit_match is None:
            return None
        try:
            package_count = Decimal(missing_unit_match.group("count"))
        except InvalidOperation:
            return None
        missing_package_unit = True
        working = working[: missing_unit_match.start()].strip()

    explicit_dose: Decimal | None = None
    dose_match = DOSE_PATTERN.search(working)
    if dose_match:
        try:
            explicit_dose = Decimal(dose_match.group("dose"))
        except InvalidOperation:
            return None
        working = working[: dose_match.start()].strip()
    elif missing_package_unit:
        # A trailing number without 包/袋 is accepted only as a review-only JNU
        # candidate when an explicit gram dose immediately precedes it.
        return None

    processing_method: str | None = None
    processing_match = PAREN_PATTERN.search(working)
    if processing_match:
        processing_method = processing_match.group("value").strip()
        working = working[: processing_match.start()].strip()

    raw_drug_name = working.strip(" ：:")
    if not raw_drug_name:
        return None

    return ParsedPrescriptionItem(
        original_text=line.strip(),
        is_basic_drug=is_basic,
        raw_drug_name=raw_drug_name,
        processing_method=processing_method,
        explicit_package_dose_g=explicit_dose,
        package_count=package_count,
        original_package_unit=original_unit,
        normalized_package_unit=normalized_unit,
    )
