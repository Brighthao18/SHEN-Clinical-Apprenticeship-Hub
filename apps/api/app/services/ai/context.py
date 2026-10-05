from __future__ import annotations

from typing import Any

from app.models.domain import Encounter
from app.services.pii import assert_deidentified, redact_text

DEIDENTIFICATION_VERSION = "v1"


class ClinicalContextBuilder:
    def build_encounter(
        self, encounter: Encounter, direct_identifiers: list[str]
    ) -> dict[str, Any]:
        facts: list[dict[str, Any]] = []
        for symptom in encounter.symptoms:
            facts.append(
                {
                    "kind": "symptom",
                    "raw": redact_text(symptom.symptom_raw, direct_identifiers),
                    "normalized": symptom.symptom_normalized,
                    "status": symptom.status.value,
                    "trend": symptom.trend.value if symptom.trend else None,
                    "source": symptom.source_type.value,
                    "document_id": str(symptom.evidence_document_id),
                }
            )
        for diagnosis in encounter.diagnoses:
            facts.append(
                {
                    "kind": "diagnosis",
                    "raw": redact_text(diagnosis.name_raw, direct_identifiers),
                    "normalized": diagnosis.name_normalized,
                    "diagnosis_type": diagnosis.diagnosis_type.value,
                    "source": diagnosis.source_type.value,
                    "document_id": str(diagnosis.evidence_document_id),
                }
            )
        prescriptions: list[dict[str, Any]] = []
        for prescription in encounter.prescriptions:
            prescriptions.append(
                {
                    "total_decoctions": prescription.total_decoctions,
                    "items": [
                        {
                            "drug": item.canonical_drug_name or item.raw_drug_name,
                            "calculated_dose_g": (
                                str(item.calculated_dose_g)
                                if item.calculated_dose_g is not None
                                else None
                            ),
                            "dose_source": item.dose_source.value,
                            "dose_scope": item.dose_scope.value,
                        }
                        for item in prescription.items
                    ],
                }
            )
        payload = {
            "deidentification_version": DEIDENTIFICATION_VERSION,
            "patient_code": encounter.patient.internal_patient_code,
            "sex": encounter.patient.sex.value,
            "age_at_encounter": encounter.age_at_encounter,
            "encounter_date": encounter.encounter_at.date().isoformat(),
            "chief_complaint": redact_text(encounter.chief_complaint_raw or "", direct_identifiers),
            "facts": facts,
            "prescriptions": prescriptions,
        }
        assert_deidentified(payload, direct_identifiers)
        return payload
