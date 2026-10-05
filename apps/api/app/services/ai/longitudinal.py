"""Phase 8 Patient Longitudinal AI Analysis.

Database records and the frozen Phase 7 Prescription Diff are the only fact
sources. This module builds a de-identified, chronologically ordered context,
defines the strict model output, and validates every evidence reference before
an AI result can be persisted.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.models.domain import Encounter, Patient
from app.models.enums import NoteKind, SourceType
from app.services.pii import assert_deidentified, redact_text
from app.services.prescription_diff import compare_prescriptions

LONGITUDINAL_CONTEXT_VERSION = "1.0.0"
LONGITUDINAL_PROMPT_VERSION = 2


class LongitudinalAnalysisRejected(ValueError):
    """Strict output or evidence validation rejected an analysis."""


class ContextProvenance(StrEnum):
    PHYSICIAN_RECORD = "physician_record"
    STUDENT_NOTE = "student_note"
    AI_EXTRACTED = "ai_extracted"
    DETERMINISTIC_PRESCRIPTION_DIFF = "deterministic_prescription_diff"
    RECORD_ABSENCE = "record_absence"
    MIXED = "mixed"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceCatalogEntry(StrictModel):
    encounter_ids: list[str] = Field(min_length=1)
    evidence_type: ContextProvenance
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecordedField(StrictModel):
    field: str
    value: str
    source: ContextProvenance
    evidence_key: str


class SymptomContext(StrictModel):
    symptom_id: str
    raw: str
    normalized: str | None
    status: str
    trend: str | None
    source: ContextProvenance
    evidence_key: str


class DiagnosisContext(StrictModel):
    diagnosis_id: str
    raw: str
    normalized: str | None
    diagnosis_type: str
    source: ContextProvenance
    evidence_key: str


class StudentNoteContext(StrictModel):
    note_id: str
    note_kind: str
    content: str
    source: Literal[ContextProvenance.STUDENT_NOTE]
    evidence_key: str


class PrescriptionItemContext(StrictModel):
    prescription_item_id: str
    raw_drug_name: str
    canonical_drug_name: str | None
    processing_method: str | None
    calculated_dose_g: str | None
    dose_scope: str
    normalization_status: str
    source: ContextProvenance
    evidence_key: str


class PrescriptionContext(StrictModel):
    prescription_id: str
    total_decoctions: int | None
    raw_instruction_text: str | None
    source: ContextProvenance
    items: list[PrescriptionItemContext]


class DeterministicPersistentMedication(StrictModel):
    name: str
    encounter_ids: list[str] = Field(min_length=2)
    evidence_keys: list[str] = Field(min_length=1)


class LongitudinalEncounterContext(StrictModel):
    encounter_id: str
    date: date
    age_at_encounter: int | None
    recorded_fields: list[RecordedField]
    symptoms: list[SymptomContext]
    diagnoses: list[DiagnosisContext]
    tongue_and_pulse_notes: list[StudentNoteContext]
    student_notes: list[StudentNoteContext]
    prescriptions: list[PrescriptionContext]
    prescription_diff_from_previous: dict[str, Any] | None


class LongitudinalPatientContext(StrictModel):
    context_version: str
    deidentification_version: str
    patient_code: str
    encounter_range: dict[str, str]
    encounters: list[LongitudinalEncounterContext] = Field(min_length=2)
    deterministic_persistent_medications: list[DeterministicPersistentMedication]
    evidence_catalog: dict[str, EvidenceCatalogEntry]


class EvidenceRef(StrictModel):
    evidence_ref_id: str = Field(min_length=1, max_length=80)
    claim: str = Field(min_length=1, max_length=2000)
    evidence_encounter_ids: list[str] = Field(min_length=1)
    evidence_type: ContextProvenance
    evidence_keys: list[str] = Field(min_length=1)


class EvidenceBackedStatement(StrictModel):
    text: str = Field(min_length=1, max_length=4000)
    evidence_ref_ids: list[str] = Field(min_length=1)


class TrajectoryPoint(StrictModel):
    encounter_id: str
    date: date
    state: Literal[
        "new",
        "persistent",
        "improved",
        "worsened",
        "resolved",
        "recorded",
        "not_recorded",
        "unknown",
    ]
    description: str = Field(min_length=1, max_length=1000)
    evidence_ref_ids: list[str] = Field(min_length=1)


class SymptomTrajectory(StrictModel):
    symptom: str = Field(min_length=1, max_length=160)
    points: list[TrajectoryPoint] = Field(min_length=1)


class DiagnosisTrajectory(StrictModel):
    diagnosis: str = Field(min_length=1, max_length=200)
    points: list[TrajectoryPoint] = Field(min_length=1)


class PrescriptionStrategyTrajectory(StrictModel):
    previous_encounter_id: str
    current_encounter_id: str
    summary: str = Field(min_length=1, max_length=2000)
    deterministic_changes: list[
        Literal[
            "ADDED",
            "REMOVED",
            "DOSE_INCREASED",
            "DOSE_DECREASED",
            "PROCESSING_CHANGED",
            "UNCHANGED",
            "UNKNOWN",
            "NORMALIZATION_PENDING",
        ]
    ] = Field(min_length=1)
    evidence_ref_ids: list[str] = Field(min_length=1)


class PersistentMedication(StrictModel):
    medication: str = Field(min_length=1, max_length=160)
    encounter_ids: list[str] = Field(min_length=2)
    evidence_ref_ids: list[str] = Field(min_length=1)


class TurningPoint(StrictModel):
    previous_encounter_id: str
    current_encounter_id: str
    recorded_changes: list[str] = Field(min_length=1)
    evidence_ref_ids: list[str] = Field(min_length=1)


class PossibleClinicalReasoning(StrictModel):
    inference: str = Field(min_length=1, max_length=2000)
    evidence_ref_ids: list[str] = Field(min_length=1)
    provenance: Literal["ai_inferred"]
    tentative: Literal[True]

    @model_validator(mode="after")
    def uses_tentative_language(self) -> PossibleClinicalReasoning:
        markers = ("可能", "提示", "从记录看", "可以理解为", "推测", "或许", "尚不能确定")
        if not any(marker in self.inference for marker in markers):
            raise ValueError("AI clinical reasoning must use explicitly tentative language")
        return self


class DiscussionQuestion(StrictModel):
    question: str = Field(min_length=1, max_length=500)
    evidence_ref_ids: list[str] = Field(min_length=1)


class PatientLongitudinalAnalysis(StrictModel):
    course_summary: EvidenceBackedStatement
    symptom_trajectory: list[SymptomTrajectory]
    diagnosis_trajectory: list[DiagnosisTrajectory]
    prescription_strategy_trajectory: list[PrescriptionStrategyTrajectory]
    persistent_medications: list[PersistentMedication]
    key_turning_points: list[TurningPoint]
    possible_clinical_reasoning: list[PossibleClinicalReasoning]
    uncertainty_and_missing_data: list[EvidenceBackedStatement]
    discussion_questions: Annotated[list[DiscussionQuestion], Field(max_length=5)]
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


LONGITUDINAL_SYSTEM_PROMPT = """你是中医师承场景的患者纵向病程分析器。
数据库记录和 Phase 7 确定性处方 Diff 是唯一事实来源，
你只负责串联、总结和提出谨慎解释。

必须遵守：
1. 按就诊时间升序分析。不得重新比较处方，不得修改或覆盖
   prescription_diff_from_previous；只可原样消费 ADDED、REMOVED、
   DOSE_INCREASED、DOSE_DECREASED、PROCESSING_CHANGED、UNCHANGED、
   UNKNOWN、NORMALIZATION_PENDING。
2. dose_change=UNKNOWN 只能表述为“剂量变化无法确定”，不得根据原文或常识猜测。
3. 未记录不等于正常、稳定或症状消失。某次未记录某项时明确写“未记录”。
4. physician_record、student_note、ai_extracted 必须分开；
   学生补录不能写成医生原话，AI抽取不能冒充医生记录。
5. 任何具体临床事实、处方变化、转折点、推测或讨论问题都必须引用输入中
   真实存在的 evidence_key。禁止输出未在输入中出现的药物、症状、诊断或原因。
6. possible_clinical_reasoning 全部是 ai_inferred，必须使用“可能、提示、
   从记录看、可以理解为、推测、或许、尚不能确定”等试探措辞。
   除非记录明确写明原因，不得写“医生因为……所以……”。
7. 重点识别有信息密度的变化和转折点，不要逐次机械复述。
   讨论问题最多 5 个，并且必须来自真实 Encounter 或 Diff。
8. 全部用中文。只输出符合 schema 的 JSON 对象。"""


def _source_for_context(source: SourceType) -> ContextProvenance | None:
    if source == SourceType.PHYSICIAN_RECORD:
        return ContextProvenance.PHYSICIAN_RECORD
    if source == SourceType.STUDENT_NOTE:
        return ContextProvenance.STUDENT_NOTE
    if source == SourceType.AI_NORMALIZED:
        # Phase 6 stores faithful AI extraction under the existing
        # ai_normalized enum; Phase 8 exposes its role explicitly.
        return ContextProvenance.AI_EXTRACTED
    return None


def _decimal(value: Any) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


class LongitudinalContextBuilder:
    def build(
        self,
        patient: Patient,
        encounters: list[Encounter],
        direct_identifiers: list[str],
        *,
        deidentification_version: str,
    ) -> LongitudinalPatientContext:
        ordered = sorted(encounters, key=lambda item: (item.encounter_at, str(item.id)))
        if len(ordered) < 2:
            raise LongitudinalAnalysisRejected("At least two encounters are required")

        catalog: dict[str, EvidenceCatalogEntry] = {}
        context_encounters: list[LongitudinalEncounterContext] = []
        diffs: list[dict[str, Any]] = []

        def register(
            key: str,
            encounter_ids: list[str],
            evidence_type: ContextProvenance,
            metadata: dict[str, Any] | None = None,
        ) -> str:
            catalog[key] = EvidenceCatalogEntry(
                encounter_ids=encounter_ids,
                evidence_type=evidence_type,
                metadata=metadata or {},
            )
            return key

        for index, encounter in enumerate(ordered):
            encounter_id = str(encounter.id)
            record_source = _source_for_context(encounter.raw_source_type)
            recorded_fields: list[RecordedField] = []
            if record_source is not None:
                for field_name, value in (
                    ("chief_complaint", encounter.chief_complaint_raw),
                    ("present_illness", encounter.present_illness_raw),
                    ("past_history", encounter.past_history_raw),
                    ("allergy", encounter.allergy_raw),
                    ("vital_signs", encounter.vital_signs_raw),
                    ("advice", encounter.advice_raw),
                    ("treatment_record", encounter.treatment_record_raw),
                ):
                    if not value:
                        continue
                    evidence_key = register(
                        f"encounters.{encounter_id}.recorded_fields.{field_name}",
                        [encounter_id],
                        record_source,
                    )
                    recorded_fields.append(
                        RecordedField(
                            field=field_name,
                            value=redact_text(value, direct_identifiers),
                            source=record_source,
                            evidence_key=evidence_key,
                        )
                    )

            symptoms: list[SymptomContext] = []
            symptom_names: list[str] = []
            for symptom in encounter.symptoms:
                source = _source_for_context(symptom.source_type)
                if source is None:
                    continue
                evidence_key = register(
                    f"encounters.{encounter_id}.symptoms.{symptom.id}",
                    [encounter_id],
                    source,
                )
                raw = redact_text(symptom.symptom_raw, direct_identifiers)
                symptom_names.append(symptom.symptom_normalized or raw)
                symptoms.append(
                    SymptomContext(
                        symptom_id=str(symptom.id),
                        raw=raw,
                        normalized=symptom.symptom_normalized,
                        status=symptom.status.value,
                        trend=symptom.trend.value if symptom.trend else None,
                        source=source,
                        evidence_key=evidence_key,
                    )
                )
            register(
                f"encounters.{encounter_id}.symptoms.index",
                [encounter_id],
                ContextProvenance.RECORD_ABSENCE,
                {"recorded_symptoms": symptom_names},
            )

            diagnoses: list[DiagnosisContext] = []
            diagnosis_names: list[str] = []
            for diagnosis in encounter.diagnoses:
                source = _source_for_context(diagnosis.source_type)
                if source is None:
                    continue
                evidence_key = register(
                    f"encounters.{encounter_id}.diagnoses.{diagnosis.id}",
                    [encounter_id],
                    source,
                )
                raw = redact_text(diagnosis.name_raw, direct_identifiers)
                diagnosis_names.append(diagnosis.name_normalized or raw)
                diagnoses.append(
                    DiagnosisContext(
                        diagnosis_id=str(diagnosis.id),
                        raw=raw,
                        normalized=diagnosis.name_normalized,
                        diagnosis_type=diagnosis.diagnosis_type.value,
                        source=source,
                        evidence_key=evidence_key,
                    )
                )
            register(
                f"encounters.{encounter_id}.diagnoses.index",
                [encounter_id],
                ContextProvenance.RECORD_ABSENCE,
                {"recorded_diagnoses": diagnosis_names},
            )

            student_notes: list[StudentNoteContext] = []
            tongue_and_pulse: list[StudentNoteContext] = []
            for note in encounter.student_notes:
                evidence_key = register(
                    f"encounters.{encounter_id}.student_notes.{note.id}",
                    [encounter_id],
                    ContextProvenance.STUDENT_NOTE,
                )
                note_context = StudentNoteContext(
                    note_id=str(note.id),
                    note_kind=note.note_kind.value,
                    content=redact_text(note.content, direct_identifiers),
                    source=ContextProvenance.STUDENT_NOTE,
                    evidence_key=evidence_key,
                )
                student_notes.append(note_context)
                if note.note_kind in {NoteKind.TONGUE, NoteKind.PULSE}:
                    tongue_and_pulse.append(note_context)

            prescriptions: list[PrescriptionContext] = []
            for prescription in encounter.prescriptions:
                prescription_source = _source_for_context(prescription.source_type)
                if prescription_source is None:
                    continue
                items: list[PrescriptionItemContext] = []
                for item in prescription.items:
                    evidence_key = register(
                        f"encounters.{encounter_id}.prescription_items.{item.id}",
                        [encounter_id],
                        prescription_source,
                    )
                    items.append(
                        PrescriptionItemContext(
                            prescription_item_id=str(item.id),
                            raw_drug_name=redact_text(item.raw_drug_name, direct_identifiers),
                            canonical_drug_name=item.canonical_drug_name,
                            processing_method=item.processing_method,
                            calculated_dose_g=_decimal(item.calculated_dose_g),
                            dose_scope=item.dose_scope.value,
                            normalization_status=item.normalization_status,
                            source=prescription_source,
                            evidence_key=evidence_key,
                        )
                    )
                prescriptions.append(
                    PrescriptionContext(
                        prescription_id=str(prescription.id),
                        total_decoctions=prescription.total_decoctions,
                        raw_instruction_text=(
                            redact_text(prescription.raw_instruction_text, direct_identifiers)
                            if prescription.raw_instruction_text
                            else None
                        ),
                        source=prescription_source,
                        items=items,
                    )
                )

            diff_payload: dict[str, Any] | None = None
            if index > 0:
                previous = ordered[index - 1]
                diff_payload = compare_prescriptions(previous, encounter)
                diff_payload["previous_encounter"][
                    "encounter_at"
                ] = previous.encounter_at.isoformat()
                diff_payload["current_encounter"][
                    "encounter_at"
                ] = encounter.encounter_at.isoformat()
                for item_index, diff_item in enumerate(diff_payload["items"]):
                    evidence_key = register(
                        (
                            f"diffs.{previous.id}.{encounter.id}.items."
                            f"{item_index}"
                        ),
                        [str(previous.id), encounter_id],
                        ContextProvenance.DETERMINISTIC_PRESCRIPTION_DIFF,
                        {
                            "change_types": diff_item["change_types"],
                            "dose_change": diff_item["dose_change"],
                            "medication": (
                                diff_item["canonical_identity"]
                                or diff_item["current_raw_name"]
                                or diff_item["previous_raw_name"]
                            ),
                        },
                    )
                    diff_item["evidence_key"] = evidence_key
                diffs.append(diff_payload)

            context_encounters.append(
                LongitudinalEncounterContext(
                    encounter_id=encounter_id,
                    date=encounter.encounter_at.date(),
                    age_at_encounter=encounter.age_at_encounter,
                    recorded_fields=recorded_fields,
                    symptoms=symptoms,
                    diagnoses=diagnoses,
                    tongue_and_pulse_notes=tongue_and_pulse,
                    student_notes=student_notes,
                    prescriptions=prescriptions,
                    prescription_diff_from_previous=diff_payload,
                )
            )

        persistent = self._persistent_medications(ordered, diffs)
        payload = LongitudinalPatientContext(
            context_version=LONGITUDINAL_CONTEXT_VERSION,
            deidentification_version=deidentification_version,
            patient_code=patient.internal_patient_code,
            encounter_range={
                "start_encounter_id": str(ordered[0].id),
                "end_encounter_id": str(ordered[-1].id),
            },
            encounters=context_encounters,
            deterministic_persistent_medications=persistent,
            evidence_catalog=catalog,
        )
        assert_deidentified(payload.model_dump(mode="json"), direct_identifiers)
        return payload

    @staticmethod
    def _persistent_medications(
        encounters: list[Encounter], diffs: list[dict[str, Any]]
    ) -> list[DeterministicPersistentMedication]:
        per_pair: list[dict[str, str]] = []
        for diff in diffs:
            matched: dict[str, str] = {}
            for item in diff["items"]:
                evidence = item["evidence"]
                if not (
                    evidence["previous_prescription_item_id"]
                    and evidence["current_prescription_item_id"]
                ):
                    continue
                label = (
                    item["canonical_identity"]
                    or item["current_raw_name"]
                    or item["previous_raw_name"]
                )
                if label:
                    matched[label] = item["evidence_key"]
            per_pair.append(matched)
        if not per_pair:
            return []
        common = set(per_pair[0])
        for pair in per_pair[1:]:
            common &= set(pair)
        encounter_ids = [str(encounter.id) for encounter in encounters]
        return [
            DeterministicPersistentMedication(
                name=name,
                encounter_ids=encounter_ids,
                evidence_keys=[pair[name] for pair in per_pair],
            )
            for name in sorted(common)
        ]


def validate_longitudinal_output(
    payload: dict[str, Any], context: LongitudinalPatientContext
) -> PatientLongitudinalAnalysis:
    try:
        analysis = PatientLongitudinalAnalysis.model_validate(payload)
    except ValidationError as exc:
        raise LongitudinalAnalysisRejected("AI output failed the strict schema") from exc

    encounter_dates = {
        encounter.encounter_id: encounter.date for encounter in context.encounters
    }
    encounter_order = list(encounter_dates)
    catalog = context.evidence_catalog
    refs: dict[str, EvidenceRef] = {}
    for ref in analysis.evidence_refs:
        if ref.evidence_ref_id in refs:
            raise LongitudinalAnalysisRejected("duplicate evidence_ref_id")
        refs[ref.evidence_ref_id] = ref
        missing_keys = [key for key in ref.evidence_keys if key not in catalog]
        if missing_keys:
            raise LongitudinalAnalysisRejected("analysis referenced an unknown evidence key")
        entries = [catalog[key] for key in ref.evidence_keys]
        expected_encounters = sorted(
            {encounter_id for entry in entries for encounter_id in entry.encounter_ids}
        )
        if sorted(set(ref.evidence_encounter_ids)) != expected_encounters:
            raise LongitudinalAnalysisRejected("evidence encounter IDs do not match their keys")
        evidence_types = {entry.evidence_type for entry in entries}
        expected_type = (
            next(iter(evidence_types))
            if len(evidence_types) == 1
            else ContextProvenance.MIXED
        )
        if ref.evidence_type != expected_type:
            raise LongitudinalAnalysisRejected("evidence type does not match source provenance")

    def require_refs(ref_ids: list[str]) -> list[EvidenceRef]:
        if any(ref_id not in refs for ref_id in ref_ids):
            raise LongitudinalAnalysisRejected("analysis used an unknown evidence_ref_id")
        return [refs[ref_id] for ref_id in ref_ids]

    require_refs(analysis.course_summary.evidence_ref_ids)
    trajectories: list[SymptomTrajectory | DiagnosisTrajectory] = [
        *analysis.symptom_trajectory,
        *analysis.diagnosis_trajectory,
    ]
    for trajectory in trajectories:
        for point in trajectory.points:
            if encounter_dates.get(point.encounter_id) != point.date:
                raise LongitudinalAnalysisRejected("trajectory date does not match Encounter")
            point_refs = require_refs(point.evidence_ref_ids)
            if point.state == "not_recorded" and any(
                ref.evidence_type != ContextProvenance.RECORD_ABSENCE for ref in point_refs
            ):
                raise LongitudinalAnalysisRejected(
                    "not_recorded trajectory points must cite record-absence evidence"
                )

    for item in analysis.prescription_strategy_trajectory:
        _validate_pair(item.previous_encounter_id, item.current_encounter_id, encounter_order)
        item_refs = require_refs(item.evidence_ref_ids)
        entries = [catalog[key] for ref in item_refs for key in ref.evidence_keys]
        diff_entries = [
            entry
            for entry in entries
            if entry.evidence_type == ContextProvenance.DETERMINISTIC_PRESCRIPTION_DIFF
        ]
        if not diff_entries:
            raise LongitudinalAnalysisRejected("prescription trajectory must cite Phase 7 diff")
        actual_changes = {
            change
            for entry in diff_entries
            for change in entry.metadata.get("change_types", [])
        }
        if any(entry.metadata.get("dose_change") == "UNKNOWN" for entry in diff_entries):
            actual_changes.add("UNKNOWN")
        if not set(item.deterministic_changes) <= actual_changes:
            raise LongitudinalAnalysisRejected("AI changed a deterministic prescription fact")

    persistent_lookup = {
        item.name: item for item in context.deterministic_persistent_medications
    }
    for medication_item in analysis.persistent_medications:
        source = persistent_lookup.get(medication_item.medication)
        if source is None or medication_item.encounter_ids != source.encounter_ids:
            raise LongitudinalAnalysisRejected("AI invented a persistent medication")
        require_refs(medication_item.evidence_ref_ids)

    for turning_item in analysis.key_turning_points:
        _validate_pair(
            turning_item.previous_encounter_id,
            turning_item.current_encounter_id,
            encounter_order,
        )
        require_refs(turning_item.evidence_ref_ids)
    for reasoning_item in analysis.possible_clinical_reasoning:
        require_refs(reasoning_item.evidence_ref_ids)
    for uncertainty_item in analysis.uncertainty_and_missing_data:
        require_refs(uncertainty_item.evidence_ref_ids)
    for question_item in analysis.discussion_questions:
        require_refs(question_item.evidence_ref_ids)
    return analysis


def _validate_pair(previous_id: str, current_id: str, encounter_order: list[str]) -> None:
    try:
        previous_index = encounter_order.index(previous_id)
        current_index = encounter_order.index(current_id)
    except ValueError as exc:
        raise LongitudinalAnalysisRejected("analysis referenced an unknown Encounter") from exc
    if current_index != previous_index + 1:
        raise LongitudinalAnalysisRejected("turning points must use adjacent Encounters")
