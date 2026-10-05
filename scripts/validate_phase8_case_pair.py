"""Explicit protected CASE002 -> CASE003 Phase 8 context validation.

This command never scans ``private_data`` and never copies clinical values to
ordinary outputs. It reads exactly the two requested protected Phase 6.1
snapshots, rebuilds their de-identified longitudinal context, consumes the
Phase 7 deterministic diff, and writes counts/lineage only to the protected
clinical-golden reports directory. It does not call an AI provider and is not
an AI-accuracy claim.
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.models.domain import (
    Diagnosis,
    Document,
    Encounter,
    Patient,
    Prescription,
    PrescriptionItem,
    SymptomObservation,
)
from app.models.enums import (
    DiagnosisType,
    DocumentType,
    DoseScope,
    DoseSource,
    ProcessingStatus,
    Sex,
    SourceType,
    SymptomStatus,
    SymptomTrend,
)
from app.services.ai.longitudinal import LongitudinalContextBuilder
from app.services.drugs import PROCESSING_NORMALIZATION, SEED_DRUGS

REQUIRED_CASE_IDS = ("CASE002", "CASE003")
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
EXPECTED_DIFF = {
    "total": 17,
    "added": 5,
    "removed": 6,
    "dose_increased": 0,
    "dose_decreased": 1,
    "unchanged": 5,
    "normalization_pending": 13,
    "unknown_dose": 1,
}


def _load_snapshot(dataset_root: Path, run_id: str, case_id: str) -> dict[str, Any]:
    path = dataset_root / "runs" / run_id / case_id / "pipeline_result_run1.json"
    if not path.is_file():
        raise FileNotFoundError(f"Missing explicit protected snapshot for {case_id}")
    return json.loads(path.read_text(encoding="utf-8"))["terminal"]


def _decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    return Decimal(str(value))


def _calculated_dose(item: dict[str, Any]) -> Decimal | None:
    explicit = _decimal(item.get("explicit_package_dose_g"))
    count = _decimal(item.get("package_count"))
    if item.get("is_direct_gram_dose") and explicit is not None:
        return explicit
    if explicit is None or count is None:
        return None
    return explicit * count


def _canonical(raw_name: str, processing: str | None) -> tuple[str | None, str | None]:
    seed = SEED_DRUGS.get(raw_name.strip())
    if seed is None:
        normalized_processing = (
            PROCESSING_NORMALIZATION.get(processing, processing) if processing else None
        )
        return None, normalized_processing
    canonical_name, seed_processing = seed
    normalized_processing = processing or seed_processing
    if normalized_processing:
        normalized_processing = PROCESSING_NORMALIZATION.get(
            normalized_processing, normalized_processing
        )
    return canonical_name, normalized_processing


def _build_encounter(patient: Patient, terminal: dict[str, Any], case_id: str) -> Encounter:
    snapshot = terminal["structured_snapshot"]
    document_id = uuid.UUID(terminal["document_id"])
    document = Document(
        id=document_id,
        original_filename=f"{case_id}.protected",
        original_file_path=f"protected/{case_id}",
        sha256=uuid.uuid5(uuid.NAMESPACE_URL, case_id).hex * 2,
        byte_size=1,
        mime_type="image/jpeg",
        document_type=DocumentType.OUTPATIENT_RECORD,
        processing_status=ProcessingStatus.ACCEPTED,
    )
    encounter = Encounter(
        id=uuid.UUID(terminal["encounter_id"]),
        patient=patient,
        primary_document=document,
        primary_document_id=document.id,
        hospital=snapshot["hospital"],
        department=snapshot.get("department"),
        physician=snapshot.get("physician"),
        encounter_at=datetime.fromisoformat(snapshot["encounter_date"]),
        age_at_encounter=snapshot.get("age"),
        chief_complaint_raw=snapshot.get("chief_complaint"),
        present_illness_raw=snapshot.get("present_illness"),
        past_history_raw=snapshot.get("past_history"),
        allergy_raw=snapshot.get("allergy"),
        vital_signs_raw=snapshot.get("vital_signs"),
        advice_raw=snapshot.get("advice"),
        treatment_record_raw=snapshot.get("treatment_record"),
        raw_source_type=SourceType.AI_NORMALIZED,
    )
    for symptom in snapshot.get("symptoms", []):
        encounter.symptoms.append(
            SymptomObservation(
                symptom_raw=symptom["raw"],
                symptom_normalized=symptom.get("normalized"),
                status=SymptomStatus(symptom["status"]),
                trend=(
                    SymptomTrend(symptom["trend"])
                    if symptom.get("trend")
                    else None
                ),
                source_type=SourceType.AI_NORMALIZED,
                evidence_document_id=document.id,
            )
        )
    for diagnosis in snapshot.get("diagnoses", []):
        encounter.diagnoses.append(
            Diagnosis(
                diagnosis_type=DiagnosisType(diagnosis["diagnosis_type"]),
                name_raw=diagnosis["raw"],
                name_normalized=diagnosis.get("normalized"),
                is_primary=diagnosis.get("is_primary", False),
                source_type=SourceType.AI_NORMALIZED,
                evidence_document_id=document.id,
            )
        )
    prescription = Prescription(
        encounter=encounter,
        evidence_document_id=document.id,
        total_decoctions=snapshot.get("total_decoctions"),
        raw_instruction_text=snapshot.get("raw_instruction_text"),
        source_type=SourceType.AI_NORMALIZED,
    )
    for line_number, item in enumerate(snapshot.get("prescription_items", []), start=1):
        canonical, processing = _canonical(
            item["raw_drug_name"], item.get("processing_method")
        )
        calculated = _calculated_dose(item)
        prescription.items.append(
            PrescriptionItem(
                id=uuid.uuid5(
                    uuid.NAMESPACE_URL, f"phase8:{case_id}:item:{line_number}"
                ),
                line_number=line_number,
                original_text=item["original_text"],
                is_basic_drug=item.get("is_basic_drug", False),
                raw_drug_name=item["raw_drug_name"],
                canonical_drug_name=canonical,
                processing_method=processing,
                explicit_package_dose_g=_decimal(item.get("explicit_package_dose_g")),
                package_count=_decimal(item.get("package_count")),
                original_package_unit=item.get("original_package_unit"),
                normalized_package_unit=item.get("normalized_package_unit"),
                calculated_dose_g=calculated,
                dose_source=(DoseSource.CALCULATED if calculated else DoseSource.UNKNOWN),
                dose_scope=DoseScope.PER_DECOCTION,
                normalization_status="normalized" if canonical else "pending",
                parser_version="protected-phase6.1-snapshot",
            )
        )
    return encounter


def run_validation(
    *, dataset_root: Path, run_id: str, case_ids: tuple[str, str], output: Path
) -> dict[str, Any]:
    if case_ids != REQUIRED_CASE_IDS:
        raise ValueError("This validator accepts only explicit CASE002 then CASE003")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("Invalid source run ID")
    terminals = [_load_snapshot(dataset_root, run_id, case_id) for case_id in case_ids]
    snapshots = [terminal["structured_snapshot"] for terminal in terminals]
    if not (
        snapshots[0]["hospital"] == snapshots[1]["hospital"]
        and snapshots[0]["medical_record_number"]
        == snapshots[1]["medical_record_number"]
    ):
        raise ValueError("Protected cases do not meet the stable same-patient rule")

    patient = Patient(
        id=uuid.uuid5(uuid.NAMESPACE_URL, "phase8-protected-CASE002-CASE003"),
        internal_patient_code="PROTECTED_CASE002_CASE003",
        sex=Sex(snapshots[0].get("sex") or "unknown"),
    )
    encounters = [
        _build_encounter(patient, terminal, case_id)
        for terminal, case_id in zip(terminals, case_ids, strict=True)
    ]
    direct_identifiers = [
        value
        for snapshot in snapshots
        for value in (snapshot.get("real_name"), snapshot.get("medical_record_number"))
        if value
    ]
    context = LongitudinalContextBuilder().build(
        patient,
        encounters,
        direct_identifiers,
        deidentification_version="v1",
    )
    diff = context.encounters[1].prescription_diff_from_previous
    if diff is None:
        raise AssertionError("CASE003 did not receive the Phase 7 diff")
    for key, expected in EXPECTED_DIFF.items():
        if diff["summary"][key] != expected:
            raise AssertionError(
                f"Protected Phase 7 diff drift for {key}: "
                f"expected {expected}, got {diff['summary'][key]}"
            )

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "phase": "8",
        "validation_kind": "protected_deidentified_context_demo",
        "source_run_id": run_id,
        "case_ids": list(case_ids),
        "same_patient_stable_match": True,
        "context_version": context.context_version,
        "deidentification_version": context.deidentification_version,
        "encounter_count": len(context.encounters),
        "phase7_algorithm_version": diff["algorithm_version"],
        "phase7_summary": diff["summary"],
        "phase7_item_fact_rows": len(diff["items"]),
        "evidence_catalog_entries": len(context.evidence_catalog),
        "context_provenance": sorted(
            {entry.evidence_type.value for entry in context.evidence_catalog.values()}
        ),
        "pii_scan_passed": True,
        "phase7_diff_consumed_without_recalculation_by_ai": True,
        "ai_provider_called": False,
        "disclaimer": (
            "Protected real-case context and deterministic-diff validation only; "
            "no clinical values were copied into this report and no AI-accuracy "
            "claim is made."
        ),
    }
    protected_root = dataset_root.resolve()
    resolved_output = output.resolve()
    if protected_root not in resolved_output.parents:
        raise ValueError("Output must remain under the protected clinical-golden root")
    resolved_output.parent.mkdir(parents=True, exist_ok=True)
    resolved_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--case-id", action="append", required=True)
    parser.add_argument(
        "--dataset-root",
        default="private_data/clinical_golden",
        type=Path,
    )
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = run_validation(
        dataset_root=args.dataset_root,
        run_id=args.source_run_id,
        case_ids=tuple(args.case_id),
        output=args.output,
    )
    print(
        json.dumps(
            {
                "status": "passed",
                "case_ids": report["case_ids"],
                "phase7_summary": report["phase7_summary"],
                "pii_scan_passed": report["pii_scan_passed"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
