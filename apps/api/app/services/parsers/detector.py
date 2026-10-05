from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from app.services.parsers.jnu import JNUHospitalOutpatientParser
from app.services.parsers.jnu_legacy import JNULegacyPrescriptionParser, flatten_mineru_markup

CURRENT_TEMPLATE = JNUHospitalOutpatientParser.template
LEGACY_TEMPLATE = JNULegacyPrescriptionParser.template
UNKNOWN_TEMPLATE = "UNKNOWN"


@dataclass(frozen=True)
class TemplateDetection:
    template: str
    score: float


def detect_template(text: str) -> TemplateDetection:
    """Route a document to exactly one template family.

    The current outpatient-record parser and the legacy prescription-slip
    parser stay fully independent; neither may accumulate layout branches
    for the other. Unknown layouts fail closed to UNRESOLVED. MinerU HTML
    table/br markup is flattened once at the routing level so table-wrapped
    slips expose their layout markers to both detectors.

    Document-type titles decide before weighted markers: a slip titled
    处方笺 without the 门(急)诊病历 record title is a prescription slip even
    when its Rp/剂/金额 markers were lost to OCR (observed on the CASE025
    family, where marker sums lose to the current template by <= 0.05).
    """

    prepared = unicodedata.normalize("NFKC", flatten_mineru_markup(text))
    current_score = JNUHospitalOutpatientParser.detect_score(prepared)
    legacy_score = JNULegacyPrescriptionParser.detect_score(prepared)
    slip_titled = "处方笺" in prepared
    record_titled = "门(急)诊病历" in prepared or "门急诊病历" in prepared
    if slip_titled and not record_titled and legacy_score >= 0.5:
        return TemplateDetection(LEGACY_TEMPLATE, legacy_score)
    if current_score >= 0.55 and current_score >= legacy_score:
        return TemplateDetection(CURRENT_TEMPLATE, current_score)
    if legacy_score >= 0.5:
        return TemplateDetection(LEGACY_TEMPLATE, legacy_score)
    return TemplateDetection(UNKNOWN_TEMPLATE, max(current_score, legacy_score, 0.1))
