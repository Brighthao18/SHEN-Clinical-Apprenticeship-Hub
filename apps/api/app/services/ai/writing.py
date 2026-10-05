"""Phase 9 Apprenticeship Writing (跟师笔记 / 医案整理).

Like Phase 8, committed database rows and the frozen Phase 7 Prescription
Diff are the only fact sources. This module builds de-identified contexts
from the already-ingested structured data, defines one strict output schema
per note type, validates every evidence reference before anything is
persisted, and renders the validated draft into the recommended section
structure. AI text is always a draft: the user finalizes 师承材料.

Phase 9 consumes Encounter facts, Student Notes and Phase 7 diffs; it never
re-runs OCR and never touches the frozen ingestion/prescription-diff layers.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.models.domain import Encounter, Patient
from app.models.enums import NoteKind, SourceType
from app.services.ai.context import DEIDENTIFICATION_VERSION
from app.services.pii import assert_deidentified, redact_text
from app.services.prescription_diff import compare_prescriptions

WRITING_CONTEXT_VERSION = "1.0.0"
TEACHER_NOTE_PROMPT_VERSION = 1
CASE_RECORD_PROMPT_VERSION = 1

_TEACHER_ORAL_KIND = NoteKind.TEACHER_ORAL.value

# Attribution language is only allowed when a recorded teacher-oral student
# note is actually cited; otherwise the AI must stay in 学习理解 mode.
_TEACHER_ATTRIBUTION_MARKERS = (
    "老师指出",
    "老师认为",
    "老师说",
    "老师讲",
    "老师强调",
    "老师的意思是",
    "师父指出",
    "师傅指出",
    "师父说",
    "师傅说",
)

_TENTATIVE_MARKERS = (
    "可能",
    "提示",
    "从记录看",
    "可以理解为",
    "推测",
    "或许",
    "尚不能确定",
    "学习理解",
    "供参考",
)


class WritingRejected(ValueError):
    """Strict output or evidence validation rejected a generated draft."""


class WritingProvenance(StrEnum):
    PHYSICIAN_RECORD = "physician_record"
    STUDENT_NOTE = "student_note"
    AI_EXTRACTED = "ai_extracted"
    DETERMINISTIC_PRESCRIPTION_DIFF = "deterministic_prescription_diff"
    RECORD_ABSENCE = "record_absence"
    MIXED = "mixed"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Context models (de-identified input for the AI)
# ---------------------------------------------------------------------------


class EvidenceCatalogEntry(StrictModel):
    encounter_ids: list[str] = Field(min_length=1)
    evidence_type: WritingProvenance
    metadata: dict[str, Any] = Field(default_factory=dict)


class RecordedField(StrictModel):
    field: str
    value: str
    source: WritingProvenance
    evidence_key: str


class SymptomContext(StrictModel):
    symptom_id: str
    raw: str
    normalized: str | None
    status: str
    trend: str | None
    source: WritingProvenance
    evidence_key: str


class DiagnosisContext(StrictModel):
    diagnosis_id: str
    raw: str
    normalized: str | None
    diagnosis_type: str
    source: WritingProvenance
    evidence_key: str


class StudentNoteContext(StrictModel):
    note_id: str
    note_kind: str
    content: str
    source: Literal[WritingProvenance.STUDENT_NOTE]
    evidence_key: str


class PrescriptionItemContext(StrictModel):
    prescription_item_id: str
    original_text: str
    raw_drug_name: str
    canonical_drug_name: str | None
    processing_method: str | None
    calculated_dose_g: str | None
    dose_scope: str
    normalization_status: str
    source: WritingProvenance
    evidence_key: str


class PrescriptionContext(StrictModel):
    prescription_id: str
    total_decoctions: int | None
    raw_instruction_text: str | None
    source: WritingProvenance
    items: list[PrescriptionItemContext]


class WritingEncounterContext(StrictModel):
    encounter_id: str
    date: date
    age_at_encounter: int | None
    recorded_fields: list[RecordedField]
    symptoms: list[SymptomContext]
    diagnoses: list[DiagnosisContext]
    student_notes: list[StudentNoteContext]
    prescriptions: list[PrescriptionContext]
    prescription_diff_from_previous: dict[str, Any] | None
    has_previous_encounter: bool


class PatientOverviewContext(StrictModel):
    patient_code: str
    sex: str


class TeacherNoteContext(StrictModel):
    context_version: str
    deidentification_version: str
    patient: PatientOverviewContext
    encounter: WritingEncounterContext
    previous_encounter: dict[str, str] | None
    evidence_catalog: dict[str, EvidenceCatalogEntry]


class CaseRecordContext(StrictModel):
    context_version: str
    deidentification_version: str
    patient: PatientOverviewContext
    encounters: list[WritingEncounterContext] = Field(min_length=1)
    evidence_catalog: dict[str, EvidenceCatalogEntry]


# ---------------------------------------------------------------------------
# Strict AI output schemas
# ---------------------------------------------------------------------------


class EvidenceRef(StrictModel):
    evidence_ref_id: str = Field(min_length=1, max_length=80)
    claim: str = Field(min_length=1, max_length=2000)
    evidence_encounter_ids: list[str] = Field(min_length=1)
    evidence_type: WritingProvenance
    evidence_keys: list[str] = Field(min_length=1)


class EvidenceBackedStatement(StrictModel):
    text: str = Field(min_length=1, max_length=2000)
    evidence_ref_ids: list[str] = Field(min_length=1)


class TentativeInterpretation(StrictModel):
    text: str = Field(min_length=1, max_length=2000)
    evidence_ref_ids: list[str] = Field(min_length=1)
    provenance: Literal["ai_inferred"]
    tentative: Literal[True]

    @model_validator(mode="after")
    def uses_tentative_language(self) -> TentativeInterpretation:
        if not any(marker in self.text for marker in _TENTATIVE_MARKERS):
            raise ValueError("AI interpretation must use explicitly tentative language")
        return self


class DiscussionQuestion(StrictModel):
    question: str = Field(min_length=1, max_length=500)
    evidence_ref_ids: list[str] = Field(min_length=1)


class TeacherNoteDraft(StrictModel):
    visit_overview: EvidenceBackedStatement
    four_examinations: list[EvidenceBackedStatement] = Field(min_length=1)
    diagnosis_and_prescription: list[EvidenceBackedStatement] = Field(min_length=1)
    # Empty is only valid for a first visit; validated against the context.
    changes_from_previous: list[EvidenceBackedStatement]
    teacher_reasoning_interpretation: list[TentativeInterpretation] = Field(min_length=1)
    student_reflection: list[TentativeInterpretation] = Field(min_length=1)
    questions_for_teacher: Annotated[
        list[DiscussionQuestion], Field(min_length=1, max_length=5)
    ]
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


class CaseVisitNarrative(StrictModel):
    encounter_id: str
    date: date
    statements: list[EvidenceBackedStatement] = Field(min_length=1)


class PrescriptionChangeSummary(StrictModel):
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


class CaseRecordDraft(StrictModel):
    basic_information: EvidenceBackedStatement
    chief_complaint: EvidenceBackedStatement
    main_symptoms: list[EvidenceBackedStatement] = Field(min_length=1)
    visit_sections: list[CaseVisitNarrative] = Field(min_length=1)
    prescription_changes: list[PrescriptionChangeSummary]
    course_evolution: EvidenceBackedStatement
    commentary: list[TentativeInterpretation] = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


TEACHER_NOTE_SYSTEM_PROMPT = """你是中医师承场景的跟师笔记草稿生成器。
数据库中的结构化就诊事实、学生补录和 Phase 7 确定性处方 Diff 是唯一事实来源。
你只负责把它们组织成一 篇结构完整、可追溯的跟师笔记草稿；最终稿由学生本人定稿。

必须遵守：
1. “发生了什么”（就诊概况、四诊及主要症状、诊断与处方、与前诊相比的变化）
   只能复述输入中的记录，不得补充任何输入中没有的症状、药物、剂量或原因。
2. 剂量与处方变化只能引用 prescription_diff_from_previous 的确定性结论；
   dose_change=UNKNOWN 只能表述为“剂量变化无法确定”。
3. 未记录不等于正常或正常与否的判断。输入未记录的内容必须写“未记录”，
   并引用对应的 record_absence 证据键。
4. teacher_reasoning_interpretation 是学生的“学习理解”，全部为 ai_inferred，
   必须使用“可能、提示、从记录看、可以理解为、推测、或许、尚不能确定、
   供参考”等试探措辞。除非引用了 teacher_oral 类型的学生补录证据键，
   否则不得出现“老师指出/老师认为/老师说”等把观点归于老师的表述。
5. student_reflection 供学生改写，同样使用试探措辞，不得虚构事实。
6. questions_for_teacher 最多 5 条，必须来自真实记录或真实 Diff。
7. 每条内容都必须引用输入 evidence_catalog 中真实存在的 evidence_key；
   evidence_ref 的 encounter_ids 必须与其引用键所属就诊一致。
8. 全部用中文。只输出符合 schema 的 JSON 对象。"""


CASE_RECORD_SYSTEM_PROMPT = """你是中医师承场景的医案整理草稿生成器。
数据库中的结构化就诊事实、学生补录和 Phase 7 确定性处方 Diff 是唯一事实来源。
你只负责把一段真实就诊记录整理成一篇去标识化、可追溯的医案草稿；最终稿由学生本人定稿。

必须遵守：
1. 就诊经过必须按 Encounter 时间顺序，且覆盖输入给出的每一次就诊；
   不得遗漏就诊、不得虚构就诊、不得合并或改写日期。
2. “发生了什么”（基本情况、主诉、主要症候、各诊经过）只能复述输入中的记录；
   未记录的内容写“未记录”，并引用对应的 record_absence 证据键。
3. prescription_changes 必须逐对引用每个相邻就诊组合的 Phase 7 确定性 Diff，
   并原样回显确定性 change 类型；dose_change=UNKNOWN 只能表述为
   “剂量变化无法确定”。不得自己重新比较处方。
4. commentary（按语）是 ai_inferred 的学习理解，必须使用“可能、提示、
   从记录看、可以理解为、推测、或许、尚不能确定、供参考”等试探措辞；
   除非引用 teacher_oral 类型的学生补录证据键，否则不得出现
   “老师指出/老师认为/老师说”等把观点归于老师的表述。
5. 基本情况只能使用去标识化信息（patient_code、性别、就诊时年龄），
   不得出现姓名、病历号、地址或联系方式。
6. 每条内容都必须引用输入 evidence_catalog 中真实存在的 evidence_key；
   evidence_ref 的 encounter_ids 必须与其引用键所属就诊一致。
7. 全部用中文。只输出符合 schema 的 JSON 对象。"""


def _source_for_context(source: SourceType) -> WritingProvenance | None:
    if source == SourceType.PHYSICIAN_RECORD:
        return WritingProvenance.PHYSICIAN_RECORD
    if source == SourceType.STUDENT_NOTE:
        return WritingProvenance.STUDENT_NOTE
    if source == SourceType.AI_NORMALIZED:
        # Phase 6 stores faithful AI extraction under ai_normalized; the
        # writing layer exposes its role explicitly.
        return WritingProvenance.AI_EXTRACTED
    return None


def _decimal(value: Any) -> str | None:
    if value is None:
        return None
    return format(value.normalize(), "f")


class WritingContextBuilder:
    """Builds de-identified note-writing contexts from committed rows only."""

    def build_teacher_note_context(
        self,
        patient: Patient,
        encounter: Encounter,
        previous_encounter: Encounter | None,
        direct_identifiers: list[str],
        *,
        deidentification_version: str = DEIDENTIFICATION_VERSION,
    ) -> TeacherNoteContext:
        catalog: dict[str, EvidenceCatalogEntry] = {}

        def register(
            key: str,
            encounter_ids: list[str],
            evidence_type: WritingProvenance,
            metadata: dict[str, Any] | None = None,
        ) -> str:
            catalog[key] = EvidenceCatalogEntry(
                encounter_ids=encounter_ids,
                evidence_type=evidence_type,
                metadata=metadata or {},
            )
            return key

        register(
            "patient.overview",
            [str(encounter.id)],
            WritingProvenance.RECORD_ABSENCE,
            {"patient_code": patient.internal_patient_code, "sex": patient.sex.value},
        )
        encounter_context = self._encounter_context(
            encounter,
            register,
            direct_identifiers,
            previous=previous_encounter,
        )
        context = TeacherNoteContext(
            context_version=WRITING_CONTEXT_VERSION,
            deidentification_version=deidentification_version,
            patient=PatientOverviewContext(
                patient_code=patient.internal_patient_code, sex=patient.sex.value
            ),
            encounter=encounter_context,
            previous_encounter=(
                {
                    "encounter_id": str(previous_encounter.id),
                    "date": previous_encounter.encounter_at.date().isoformat(),
                }
                if previous_encounter is not None
                else None
            ),
            evidence_catalog=catalog,
        )
        assert_deidentified(context.model_dump(mode="json"), direct_identifiers)
        return context

    def build_case_record_context(
        self,
        patient: Patient,
        encounters: list[Encounter],
        direct_identifiers: list[str],
        *,
        deidentification_version: str = DEIDENTIFICATION_VERSION,
    ) -> CaseRecordContext:
        ordered = sorted(encounters, key=lambda item: (item.encounter_at, str(item.id)))
        if not ordered:
            raise WritingRejected("At least one encounter is required")
        catalog: dict[str, EvidenceCatalogEntry] = {}

        def register(
            key: str,
            encounter_ids: list[str],
            evidence_type: WritingProvenance,
            metadata: dict[str, Any] | None = None,
        ) -> str:
            catalog[key] = EvidenceCatalogEntry(
                encounter_ids=encounter_ids,
                evidence_type=evidence_type,
                metadata=metadata or {},
            )
            return key

        register(
            "patient.overview",
            [str(ordered[0].id)],
            WritingProvenance.RECORD_ABSENCE,
            {"patient_code": patient.internal_patient_code, "sex": patient.sex.value},
        )
        context_encounters = []
        for index, encounter in enumerate(ordered):
            previous = ordered[index - 1] if index > 0 else None
            context_encounters.append(
                self._encounter_context(encounter, register, direct_identifiers, previous=previous)
            )
        context = CaseRecordContext(
            context_version=WRITING_CONTEXT_VERSION,
            deidentification_version=deidentification_version,
            patient=PatientOverviewContext(
                patient_code=patient.internal_patient_code, sex=patient.sex.value
            ),
            encounters=context_encounters,
            evidence_catalog=catalog,
        )
        assert_deidentified(context.model_dump(mode="json"), direct_identifiers)
        return context

    def _encounter_context(
        self,
        encounter: Encounter,
        register,
        direct_identifiers: list[str],
        *,
        previous: Encounter | None,
    ) -> WritingEncounterContext:
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
            WritingProvenance.RECORD_ABSENCE,
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
            WritingProvenance.RECORD_ABSENCE,
            {"recorded_diagnoses": diagnosis_names},
        )

        student_notes: list[StudentNoteContext] = []
        for note in encounter.student_notes:
            evidence_key = register(
                f"encounters.{encounter_id}.student_notes.{note.id}",
                [encounter_id],
                WritingProvenance.STUDENT_NOTE,
                {"note_kind": note.note_kind.value},
            )
            student_notes.append(
                StudentNoteContext(
                    note_id=str(note.id),
                    note_kind=note.note_kind.value,
                    content=redact_text(note.content, direct_identifiers),
                    source=WritingProvenance.STUDENT_NOTE,
                    evidence_key=evidence_key,
                )
            )

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
                        original_text=redact_text(item.original_text, direct_identifiers),
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
        register(
            f"encounters.{encounter_id}.prescriptions.index",
            [encounter_id],
            WritingProvenance.RECORD_ABSENCE,
            {"prescription_count": len(prescriptions)},
        )

        diff_payload: dict[str, Any] | None = None
        if previous is not None:
            diff_payload = compare_prescriptions(previous, encounter)
            diff_payload["previous_encounter"]["encounter_at"] = (
                previous.encounter_at.isoformat()
            )
            diff_payload["current_encounter"]["encounter_at"] = encounter.encounter_at.isoformat()
            change_types = sorted(
                {change for item in diff_payload["items"] for change in item["change_types"]}
            )
            dose_unknown = any(
                item["dose_change"] == "UNKNOWN" for item in diff_payload["items"]
            )
            summary_key = register(
                f"diffs.{previous.id}.{encounter_id}.summary",
                [str(previous.id), encounter_id],
                WritingProvenance.DETERMINISTIC_PRESCRIPTION_DIFF,
                {"change_types": change_types, "dose_unknown": dose_unknown},
            )
            diff_payload["evidence_key"] = summary_key
            for item_index, diff_item in enumerate(diff_payload["items"]):
                evidence_key = register(
                    f"diffs.{previous.id}.{encounter_id}.items.{item_index}",
                    [str(previous.id), encounter_id],
                    WritingProvenance.DETERMINISTIC_PRESCRIPTION_DIFF,
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

        return WritingEncounterContext(
            encounter_id=encounter_id,
            date=encounter.encounter_at.date(),
            age_at_encounter=encounter.age_at_encounter,
            recorded_fields=recorded_fields,
            symptoms=symptoms,
            diagnoses=diagnoses,
            student_notes=student_notes,
            prescriptions=prescriptions,
            prescription_diff_from_previous=diff_payload,
            has_previous_encounter=previous is not None,
        )


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------


class _ValidatedRefs:
    """Resolved evidence refs shared by both note-type validators."""

    def __init__(self, refs: list[EvidenceRef], catalog: dict[str, EvidenceCatalogEntry]) -> None:
        self.refs = refs
        self.catalog = catalog
        self.by_id: dict[str, EvidenceRef] = {}
        for ref in refs:
            if ref.evidence_ref_id in self.by_id:
                raise WritingRejected("duplicate evidence_ref_id")
            missing = [key for key in ref.evidence_keys if key not in catalog]
            if missing:
                raise WritingRejected("analysis referenced an unknown evidence key")
            entries = [catalog[key] for key in ref.evidence_keys]
            expected_encounters = sorted(
                {encounter_id for entry in entries for encounter_id in entry.encounter_ids}
            )
            if sorted(set(ref.evidence_encounter_ids)) != expected_encounters:
                raise WritingRejected("evidence encounter IDs do not match their keys")
            evidence_types = {entry.evidence_type for entry in entries}
            expected_type = (
                next(iter(evidence_types))
                if len(evidence_types) == 1
                else WritingProvenance.MIXED
            )
            if ref.evidence_type != expected_type:
                raise WritingRejected("evidence type does not match source provenance")
            self.by_id[ref.evidence_ref_id] = ref

    def resolve(self, ref_ids: list[str]) -> list[EvidenceRef]:
        if any(ref_id not in self.by_id for ref_id in ref_ids):
            raise WritingRejected("draft used an unknown evidence_ref_id")
        return [self.by_id[ref_id] for ref_id in ref_ids]

    def cites_teacher_oral_note(self, ref_ids: list[str]) -> bool:
        for ref in self.resolve(ref_ids):
            for key in ref.evidence_keys:
                entry = self.catalog[key]
                if entry.metadata.get("note_kind") == _TEACHER_ORAL_KIND:
                    return True
        return False


def _reject_teacher_attribution_without_record(
    texts: list[str], ref_id_groups: list[list[str]], refs: _ValidatedRefs
) -> None:
    for text, ref_ids in zip(texts, ref_id_groups, strict=False):
        if not any(marker in text for marker in _TEACHER_ATTRIBUTION_MARKERS):
            continue
        if not refs.cites_teacher_oral_note(ref_ids):
            raise WritingRejected(
                "teacher-attribution wording requires a cited teacher_oral student note"
            )


def validate_teacher_note_output(
    payload: dict[str, Any], context: TeacherNoteContext
) -> TeacherNoteDraft:
    try:
        draft = TeacherNoteDraft.model_validate(payload)
    except ValidationError as exc:
        raise WritingRejected("AI output failed the strict schema") from exc

    refs = _ValidatedRefs(draft.evidence_refs, context.evidence_catalog)
    for statement in (
        draft.visit_overview,
        *draft.four_examinations,
        *draft.diagnosis_and_prescription,
    ):
        refs.resolve(statement.evidence_ref_ids)

    if context.previous_encounter is None:
        if draft.changes_from_previous:
            raise WritingRejected("changes_from_previous must be empty without a previous visit")
    else:
        if not draft.changes_from_previous:
            raise WritingRejected("changes_from_previous must compare against the previous visit")
        for statement in draft.changes_from_previous:
            resolved = refs.resolve(statement.evidence_ref_ids)
            if not any(
                ref.evidence_type == WritingProvenance.DETERMINISTIC_PRESCRIPTION_DIFF
                for ref in resolved
            ):
                raise WritingRejected(
                    "changes_from_previous must cite the Phase 7 deterministic diff"
                )

    for item in (*draft.teacher_reasoning_interpretation, *draft.student_reflection):
        refs.resolve(item.evidence_ref_ids)
    _reject_teacher_attribution_without_record(
        [item.text for item in draft.teacher_reasoning_interpretation],
        [item.evidence_ref_ids for item in draft.teacher_reasoning_interpretation],
        refs,
    )
    for question in draft.questions_for_teacher:
        refs.resolve(question.evidence_ref_ids)
    return draft


def validate_case_record_output(
    payload: dict[str, Any], context: CaseRecordContext
) -> CaseRecordDraft:
    try:
        draft = CaseRecordDraft.model_validate(payload)
    except ValidationError as exc:
        raise WritingRejected("AI output failed the strict schema") from exc

    refs = _ValidatedRefs(draft.evidence_refs, context.evidence_catalog)
    for statement in (
        draft.basic_information,
        draft.chief_complaint,
        *draft.main_symptoms,
        draft.course_evolution,
    ):
        refs.resolve(statement.evidence_ref_ids)

    expected_encounters = [encounter.encounter_id for encounter in context.encounters]
    if [section.encounter_id for section in draft.visit_sections] != expected_encounters:
        raise WritingRejected("visit_sections must cover the selected encounters in order")
    dates_by_encounter = {
        encounter.encounter_id: encounter.date for encounter in context.encounters
    }
    for section in draft.visit_sections:
        if dates_by_encounter.get(section.encounter_id) != section.date:
            raise WritingRejected("visit date does not match the Encounter")
        for statement in section.statements:
            refs.resolve(statement.evidence_ref_ids)

    diff_keys_by_pair: dict[tuple[str, str], set[str]] = {}
    for encounter in context.encounters:
        diff = encounter.prescription_diff_from_previous
        if diff is None:
            continue
        pair = (diff["previous_encounter"]["id"], encounter.encounter_id)
        keys = {diff["evidence_key"]}
        keys |= {item["evidence_key"] for item in diff["items"]}
        diff_keys_by_pair[pair] = keys

    expected_pairs = list(zip(expected_encounters, expected_encounters[1:], strict=False))
    actual_pairs = [
        (item.previous_encounter_id, item.current_encounter_id)
        for item in draft.prescription_changes
    ]
    if actual_pairs != expected_pairs:
        raise WritingRejected("prescription_changes must cover each adjacent pair in order")

    for item in draft.prescription_changes:
        resolved = refs.resolve(item.evidence_ref_ids)
        cited_diff_keys: set[str] = set()
        for ref in resolved:
            if ref.evidence_type != WritingProvenance.DETERMINISTIC_PRESCRIPTION_DIFF:
                continue
            cited_diff_keys.update(ref.evidence_keys)
        pair = (item.previous_encounter_id, item.current_encounter_id)
        if not cited_diff_keys or not cited_diff_keys <= diff_keys_by_pair.get(pair, set()):
            raise WritingRejected(
                "prescription_changes must cite the matching adjacent-pair Phase 7 diff"
            )
        actual_changes = {
            change
            for key in cited_diff_keys
            for change in context.evidence_catalog[key].metadata.get("change_types", [])
        }
        entries = [context.evidence_catalog[key] for key in cited_diff_keys]
        if any(entry.metadata.get("dose_unknown") for entry in entries):
            actual_changes.add("UNKNOWN")
        if not set(item.deterministic_changes) <= actual_changes:
            raise WritingRejected("AI changed a deterministic prescription fact")

    for commentary_item in draft.commentary:
        refs.resolve(commentary_item.evidence_ref_ids)
    _reject_teacher_attribution_without_record(
        [item.text for item in draft.commentary],
        [item.evidence_ref_ids for item in draft.commentary],
        refs,
    )
    return draft


# ---------------------------------------------------------------------------
# Deterministic rendering of validated drafts
# ---------------------------------------------------------------------------

_VISIT_LABELS = ("初诊", "二诊", "三诊", "四诊", "五诊", "六诊", "七诊", "八诊", "九诊", "十诊")


def _visit_label(index: int) -> str:
    if index < len(_VISIT_LABELS):
        return _VISIT_LABELS[index]
    return f"第{index + 1}诊"


def _statements(items: list[Any]) -> list[str]:
    # EvidenceBackedStatement and TentativeInterpretation both carry .text.
    return [f"- {item.text}" for item in items]


def render_teacher_note(draft: TeacherNoteDraft, context: TeacherNoteContext) -> str:
    encounter = context.encounter
    lines: list[str] = [
        "一、就诊概况",
        draft.visit_overview.text,
        "",
        "二、四诊及主要症状",
        *_statements(draft.four_examinations),
        "",
        "三、诊断与处方",
        *_statements(draft.diagnosis_and_prescription),
        "",
        "四、与前诊相比的变化",
    ]
    if draft.changes_from_previous:
        lines += _statements(draft.changes_from_previous)
    else:
        lines.append("本次为首次就诊（或所选范围之前无就诊记录），暂无可比较的前诊。")
    lines += [
        "",
        "五、老师诊疗思路的学习理解",
        "（本节为 AI 辅助整理的学习理解，非老师亲口观点，请核实后采用）",
        *_statements(draft.teacher_reasoning_interpretation),
        "",
        "六、我的思考",
        *_statements(draft.student_reflection),
        "",
        "七、值得进一步请教的问题",
    ]
    for index, question in enumerate(draft.questions_for_teacher, start=1):
        lines.append(f"{index}. {question.question}")
    lines += [
        "",
        "――――",
        "内容依据（Encounter IDs）：",
        f"- {encounter.encounter_id}（{encounter.date.isoformat()}）",
    ]
    if context.previous_encounter is not None:
        lines.append(
            f"- {context.previous_encounter['encounter_id']}"
            f"（{context.previous_encounter['date']}，用于处方对比）"
        )
    return "\n".join(lines)


def render_case_record(draft: CaseRecordDraft, context: CaseRecordContext) -> str:
    lines: list[str] = [
        "【基本情况】",
        draft.basic_information.text,
        "",
        "【主诉】",
        draft.chief_complaint.text,
        "",
        "【现病史/主要症候】",
        *_statements(draft.main_symptoms),
        "",
    ]
    for index, section in enumerate(draft.visit_sections):
        heading = f"【{_visit_label(index)}】 {section.date.isoformat()}"
        lines += [heading, *_statements(section.statements), ""]
    lines.append("【方药变化】")
    for item in draft.prescription_changes:
        changes = "、".join(item.deterministic_changes)
        lines.append(f"- {item.summary}（确定性变化：{changes}）")
    if not draft.prescription_changes:
        lines.append("- 仅一次就诊，无相邻处方对比。")
    lines += [
        "",
        "【病程演变】",
        draft.course_evolution.text,
        "",
        "【按语】",
        "（按语为 AI 辅助理解，供参考核实）",
        *_statements(draft.commentary),
        "",
        "――――",
        "内容依据（Encounter IDs）：",
    ]
    for encounter in context.encounters:
        lines.append(f"- {encounter.encounter_id}（{encounter.date.isoformat()}）")
    return "\n".join(lines)
