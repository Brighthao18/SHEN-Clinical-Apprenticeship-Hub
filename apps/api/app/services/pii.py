from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
ID_CARD_RE = re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\w)")
MRN_RE = re.compile(r"(?:病历号|门诊号|病案号)\s*[:：]?\s*[A-Za-z0-9_-]{2,40}")
ADDRESS_RE = re.compile(r"(?:地址|住址)\s*[:：]?\s*[^\n,，;；]{3,80}")

# Structural identifiers that legitimately appear in serialized payloads but
# can coincidentally contain long digit runs (a random UUID hex segment can
# hold 11 consecutive decimal digits). They are replaced before pattern
# scanning so inspection stays about content, not about random IDs.
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
LONG_HEX_RE = re.compile(r"\b[0-9a-fA-F]{32,}\b")


@dataclass(frozen=True)
class PIIFinding:
    kind: str
    sample: str


class PIILeakError(ValueError):
    pass


def redact_text(text: str, direct_identifiers: Iterable[str] = ()) -> str:
    redacted = text
    for value in sorted({v for v in direct_identifiers if v}, key=len, reverse=True):
        redacted = redacted.replace(value, "[已脱敏]")
    redacted = PHONE_RE.sub("[手机号已脱敏]", redacted)
    redacted = ID_CARD_RE.sub("[身份证已脱敏]", redacted)
    redacted = MRN_RE.sub("[病历号已脱敏]", redacted)
    redacted = ADDRESS_RE.sub("[位置已脱敏]", redacted)
    return redacted


def find_pii(text: str, direct_identifiers: Iterable[str] = ()) -> list[PIIFinding]:
    findings: list[PIIFinding] = []
    for kind, pattern in (
        ("phone", PHONE_RE),
        ("id_card", ID_CARD_RE),
        ("medical_record_number", MRN_RE),
        ("address", ADDRESS_RE),
    ):
        match = pattern.search(text)
        if match:
            findings.append(PIIFinding(kind, "[REDACTED]"))
    for value in {v for v in direct_identifiers if v}:
        if value in text:
            findings.append(PIIFinding("direct_identifier", "[REDACTED]"))
    return findings


def assert_deidentified(payload: dict[str, Any], direct_identifiers: Iterable[str] = ()) -> None:
    serialized = _scrub_structural_tokens(str(payload))
    findings = find_pii(serialized, direct_identifiers)
    if findings:
        kinds = ", ".join(sorted({finding.kind for finding in findings}))
        raise PIILeakError(f"Cloud context failed PII inspection: {kinds}")


def _scrub_structural_tokens(text: str) -> str:
    text = UUID_RE.sub("[标识符]", text)
    return LONG_HEX_RE.sub("[哈希]", text)
