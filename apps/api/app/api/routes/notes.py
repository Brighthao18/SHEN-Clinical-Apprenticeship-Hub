"""Phase 9 Apprenticeship Writing API (跟师笔记 / 医案整理).

AI output is always a draft version (``ai_draft``) appended to an
append-only version chain; the user's own text is stored as separate
``user_original`` / ``user_draft`` / ``user_final`` versions and is never
rewritten by the AI. Generation consumes only committed structured rows
plus the frozen Phase 7 diff and fails closed without a configured
``apprenticeship_writing`` provider or when the strict schema/evidence
validation rejects the AI output.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Iterable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import String, and_, func, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, load_only, selectinload

from app.core.security import CurrentUser
from app.db.session import get_db
from app.models.domain import (
    ApprenticeshipNoteVersion,
    Encounter,
    Patient,
    Prescription,
    PromptTemplate,
    PromptVersion,
)
from app.models.enums import AITaskType, ApprenticeshipNoteType, NoteVersionType
from app.services.ai.context import DEIDENTIFICATION_VERSION
from app.services.ai.gateway import (
    AIProviderError,
    context_hash,
    get_apprenticeship_writing_provider,
)
from app.services.ai.writing import (
    CASE_RECORD_PROMPT_VERSION,
    CASE_RECORD_SYSTEM_PROMPT,
    TEACHER_NOTE_PROMPT_VERSION,
    TEACHER_NOTE_SYSTEM_PROMPT,
    WRITING_CONTEXT_VERSION,
    CaseRecordContext,
    CaseRecordDraft,
    TeacherNoteContext,
    TeacherNoteDraft,
    WritingContextBuilder,
    WritingRejected,
    render_case_record,
    render_teacher_note,
    validate_case_record_output,
    validate_teacher_note_output,
)
from app.services.audit import append_audit_event
from app.services.prescription_diff import (
    PRESCRIPTION_DIFF_ALGORITHM_VERSION,
    AmbiguousPrescriptionDiffError,
)

router = APIRouter(tags=["notes"])
DbSession = Annotated[Session, Depends(get_db)]

EVIDENCE_SNAPSHOT_SET_VERSION = 1


class CaseRecordGenerateRequest(BaseModel):
    start_encounter_id: uuid.UUID | None = None
    end_encounter_id: uuid.UUID | None = None


class NoteRevisionCreate(BaseModel):
    content: str = Field(min_length=1)
    final: bool = False
    based_on_version_id: uuid.UUID | None = None
    request_id: uuid.UUID | None = None
    # 采用新资料整理：显式提供新依据的就诊集合（排序去重后写入新版本，并
    # 按当前数据库重算依据快照与上下文哈希）。缺省 = 修改文字，逐字继承所
    # 基于版本的依据。这是“人工整理也能更新依据”的接口基础。
    # 空范围在 schema 层与预览端点同样拒绝（min_length=1），约束保持一致。
    evidence_encounter_ids: list[uuid.UUID] | None = Field(default=None, min_length=1)
    # 预览凭证：采用请求必须携带 e-preview 响应返回的 preview_token，服务端
    # 按当前数据库重算后比对，不一致即拒绝（用户确认的快照必须就是入库的
    # 快照）。已按请求标识命中的历史重放不重新校验凭证。
    preview_token: str | None = None


class EvidencePreviewRequest(BaseModel):
    # 预览必须绑定“用户正在查看的版本”：不传就是含糊预览（可能按最新版本
    # 计算，而采用按所选版本执行），一律 422。
    based_on_version_id: uuid.UUID
    evidence_encounter_ids: list[uuid.UUID] = Field(min_length=1)


def _evidence_preview_token(
    note_id: uuid.UUID,
    based_on_version_id: uuid.UUID,
    evidence_ids: list[str],
    snapshot: dict[str, Any],
) -> str:
    """预览凭证：把“这次预览的前提”整体固化为一个可重算的哈希。

    覆盖笔记、所基于版本、规范化就诊集合、按当时数据库算出的上下文哈希，
    以及各层规则版本。采用时按当前数据库重新计算并比对：期间材料、规则
    或范围只要有一处变化，比对即失败，用户必须重新预览。无状态、可重算，
    不引入任何存储。
    """

    canonical = "|".join(
        [
            "evidence-preview-v1",
            str(note_id),
            str(based_on_version_id),
            ",".join(sorted(evidence_ids)),
            str(snapshot.get("set_version")),
            str(snapshot.get("context_version")),
            str(snapshot.get("prescription_diff_algorithm_version")),
            str(snapshot.get("deidentification_version")),
            str(snapshot.get("context_hash")),
        ]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class UserOriginalNoteCreate(BaseModel):
    note_type: ApprenticeshipNoteType
    content: str = Field(min_length=1)
    title: str | None = Field(default=None, max_length=160)
    encounter_ids: list[uuid.UUID] = Field(min_length=1)


def _patient_with_writing_context(db: Session, patient_id: uuid.UUID) -> Patient | None:
    return db.scalar(
        select(Patient)
        .where(Patient.id == patient_id)
        .options(
            selectinload(Patient.identities),
            selectinload(Patient.encounters).selectinload(Encounter.symptoms),
            selectinload(Patient.encounters).selectinload(Encounter.diagnoses),
            selectinload(Patient.encounters)
            .selectinload(Encounter.prescriptions)
            .selectinload(Prescription.items),
            selectinload(Patient.encounters).selectinload(Encounter.student_notes),
        )
    )


def _direct_identifiers(patient: Patient) -> list[str]:
    values: list[str] = []
    for identity in patient.identities:
        values.extend(
            value
            for value in (
                identity.real_name,
                identity.phone,
                identity.id_card,
                identity.address,
                identity.hospital_medical_record_number,
            )
            if value
        )
    return values


def _ordered_encounters(patient: Patient) -> list[Encounter]:
    return sorted(patient.encounters, key=lambda item: (item.encounter_at, str(item.id)))


def _require_encounter_in_patient(patient: Patient, encounter_id: uuid.UUID) -> Encounter:
    for encounter in patient.encounters:
        if encounter.id == encounter_id:
            return encounter
    raise HTTPException(status_code=422, detail="所选就诊不属于该患者")


def _select_encounter_range(
    patient: Patient, start: uuid.UUID | None, end: uuid.UUID | None
) -> list[Encounter]:
    ordered = _ordered_encounters(patient)
    if start is None and end is None:
        return ordered
    if start is None or end is None:
        raise HTTPException(status_code=422, detail="开始和结束就诊必须同时选择")
    encounter_ids = [encounter.id for encounter in ordered]
    try:
        start_index = encounter_ids.index(start)
        end_index = encounter_ids.index(end)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="所选就诊不属于该患者") from exc
    if start_index > end_index:
        raise HTTPException(status_code=422, detail="开始就诊不能晚于结束就诊")
    return ordered[start_index : end_index + 1]


def _writing_prompt_version(
    db: Session,
    *,
    task_type: AITaskType,
    version_number: int,
    prompt_text: str,
    output_schema: dict[str, Any],
) -> PromptVersion:
    template = db.scalar(select(PromptTemplate).where(PromptTemplate.key == task_type.value))
    if template is None:
        template = PromptTemplate(
            key=task_type.value,
            task_type=task_type,
            description="Phase 9 evidence-grounded apprenticeship note drafting",
        )
        db.add(template)
        db.flush()
    prompt_version = db.scalar(
        select(PromptVersion).where(
            PromptVersion.template_id == template.id,
            PromptVersion.version == version_number,
        )
    )
    if prompt_version is None:
        prompt_version = PromptVersion(
            template_id=template.id,
            version=version_number,
            prompt_text=prompt_text,
            available_context={
                "committed_rows_only": True,
                "provenance": ["physician_record", "student_note", "ai_extracted"],
                "phase7_prescription_diff": "required_direct_input",
                "patient_identity": False,
                "ocr_reinvocation": False,
            },
            retrieval_policy={
                "scope": "explicit_note_evidence_encounters",
                "single_call": True,
                "rag": False,
            },
            output_schema=output_schema,
            status="active",
        )
        db.add(prompt_version)
        db.flush()
    return prompt_version


_EVIDENCE_SEPARATOR = "――――"
_EVIDENCE_HEADING = "内容依据（Encounter IDs）："
# A system evidence appendix is only stripped when it is the trailing block:
# separator, heading, then "- " entry lines reaching the end of the text.
# User-written separators or headings mid-document are left alone.
_EVIDENCE_TAIL_RE = re.compile(
    r"\n*"
    + re.escape(_EVIDENCE_SEPARATOR)
    + r"[ \t]*\n"
    + re.escape(_EVIDENCE_HEADING)
    + r"\n(?:- [^\n]*\n)*- [^\n]*$"
)


def _strip_system_evidence_appendix(content: str) -> str:
    return _EVIDENCE_TAIL_RE.sub("", content.rstrip()).rstrip()


def _encounter_date_map(db: Session, evidence_ids: Iterable[str]) -> dict[str, Any]:
    """Look up evidence-encounter dates in ONE query for the given ids."""

    ids = {value for value in evidence_ids if value}
    if not ids:
        return {}
    rows = db.execute(
        select(Encounter.id, Encounter.encounter_at).where(
            Encounter.id.in_([uuid.UUID(value) for value in ids])
        )
    ).all()
    return {str(row.id): row.encounter_at for row in rows}


def _patient_encounter_rows(
    db: Session, patient_ids: set[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[uuid.UUID, Any]]]:
    """Load (id, encounter_at) per patient in ONE query for scope hints."""

    if not patient_ids:
        return {}
    grouped: dict[uuid.UUID, list[tuple[uuid.UUID, Any]]] = {}
    for patient_id, encounter_id, encounter_at in db.execute(
        select(Encounter.patient_id, Encounter.id, Encounter.encounter_at).where(
            Encounter.patient_id.in_(patient_ids)
        )
    ).all():
        grouped.setdefault(patient_id, []).append((encounter_id, encounter_at))
    return grouped


def _evidence_window(
    patient_encounters: list[tuple[uuid.UUID, Any]],
    evidence_ids: list[str],
    evidence_dates: dict[str, Any],
) -> dict[str, Any]:
    """Scope facts for “旧结果说明适用范围”.

    Reports the newest evidence encounter date and how many of the patient's
    LATER encounters are not part of the note's evidence. Consumers show this
    as “此后有 N 次新就诊未包含”; nothing is ever regenerated automatically.
    """

    dates = [
        evidence_dates[value] for value in evidence_ids if evidence_dates.get(value) is not None
    ]
    latest = max(dates) if dates else None
    if latest is None:
        return {"latest_evidence_encounter_at": None, "later_encounter_count": 0}
    evidence_set = set(evidence_ids)
    later = sum(
        1
        for encounter_id, encounter_at in patient_encounters
        if encounter_at is not None
        and encounter_at > latest
        and str(encounter_id) not in evidence_set
    )
    return {"latest_evidence_encounter_at": latest, "later_encounter_count": later}


def _patient_encounter_picker_rows(db: Session, patient_id: uuid.UUID) -> list[dict[str, Any]]:
    """Chronological encounter list for the evidence-range picker (id/date/
    chief complaint only — positioning plus the title line the patient
    workspace already shows this user)."""

    rows = db.execute(
        select(Encounter.id, Encounter.encounter_at, Encounter.chief_complaint_raw)
        .where(Encounter.patient_id == patient_id)
        .order_by(Encounter.encounter_at.asc(), Encounter.id.asc())
    ).all()
    return [
        {
            "id": str(row.id),
            "encounter_at": row.encounter_at,
            "chief_complaint_raw": row.chief_complaint_raw,
        }
        for row in rows
    ]


def _evidence_status_for_version(
    db: Session, latest: ApprenticeshipNoteVersion
) -> dict[str, Any] | None:
    """Compare “the materials this version adopted” with the CURRENT database.

    ``matches_current_data=False`` means recomputing now yields DIFFERENT
    materials (encounter content edited, or the context/diff spec versions
    moved on) — a notice that re-verification is available when the user
    chooses, never a judgement that the historical work is wrong. Ambiguous
    prescription diffs make even the comparison impossible (fail closed as
    ``ambiguous``); unresolvable evidence sets (encounter gone, constraint
    broken) report ``unresolvable``. Read-only: nothing is persisted.
    """

    evidence_ids = [value for value in latest.evidence_encounter_ids if value]
    if not evidence_ids:
        return None
    patient = _patient_with_writing_context(db, latest.patient_id)
    if patient is None:
        return None
    try:
        encounters = [
            _require_encounter_in_patient(patient, uuid.UUID(value)) for value in evidence_ids
        ]
    except HTTPException:
        return {"status": "unresolvable"}
    if latest.note_type == ApprenticeshipNoteType.TEACHER_NOTE and len(encounters) != 1:
        return {"status": "unresolvable"}
    try:
        snapshot = _build_evidence_snapshot(
            latest.note_type, patient, encounters, _direct_identifiers(patient)
        )
    except AmbiguousPrescriptionDiffError:
        return {"status": "ambiguous"}
    return {
        "status": "ok",
        "matches_current_data": snapshot["context_hash"] == latest.source_context_hash,
    }


def _evidence_appendix(
    db: Session,
    evidence_encounter_ids: list[str],
    evidence_dates: dict[str, Any] | None = None,
) -> str:
    if not evidence_encounter_ids:
        return ""
    dates = (
        evidence_dates
        if evidence_dates is not None
        else _encounter_date_map(db, evidence_encounter_ids)
    )
    lines = [_EVIDENCE_SEPARATOR, _EVIDENCE_HEADING]
    for value in evidence_encounter_ids:
        encounter_at = dates.get(value)
        entry = (
            f"- {value}（{encounter_at.date().isoformat()}）"
            if encounter_at is not None
            else f"- {value}"
        )
        lines.append(entry)
    return "\n".join(lines)


def _evidence_snapshot_summary(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    """Small read model of a version's evidence snapshot for list/history UI.

    None means the version predates snapshots: the UI must label it honestly
    (就诊 ID 与哈希仍在，材料不可重建) — never backfilled from current data.
    """

    if snapshot is None:
        return None
    context = snapshot.get("context") or {}
    if not isinstance(context, dict):
        encounter_contexts: list[dict[str, Any]] = []
    elif isinstance(context.get("encounters"), list):
        # 医案整理：多就诊上下文。
        encounter_contexts = [item for item in context["encounters"] if isinstance(item, dict)]
    elif isinstance(context.get("encounter"), dict):
        # 跟师笔记：单就诊上下文。
        encounter_contexts = [context["encounter"]]
    else:
        encounter_contexts = []

    def _count(items: Any) -> int:
        return len(items) if isinstance(items, list) else 0

    return {
        "set_version": snapshot.get("set_version"),
        "context_version": snapshot.get("context_version"),
        "prescription_diff_algorithm_version": snapshot.get(
            "prescription_diff_algorithm_version"
        ),
        "context_hash": snapshot.get("context_hash"),
        "encounter_count": len(encounter_contexts),
        "symptom_count": sum(_count(item.get("symptoms")) for item in encounter_contexts),
        "diagnosis_count": sum(_count(item.get("diagnoses")) for item in encounter_contexts),
        "prescription_item_count": sum(
            _count(prescription.get("items"))
            for item in encounter_contexts
            for prescription in (
                item.get("prescriptions") if isinstance(item.get("prescriptions"), list) else []
            )
        ),
        "student_note_count": sum(
            _count(item.get("student_notes")) for item in encounter_contexts
        ),
        "has_prescription_diff": any(
            item.get("prescription_diff_from_previous") is not None
            for item in encounter_contexts
        ),
    }


def _serialize_version(
    db: Session,
    row: ApprenticeshipNoteVersion,
    evidence_dates: dict[str, Any] | None = None,
    *,
    summary: bool = False,
) -> dict[str, Any]:
    """Read model for one note version.

    The stored ``content`` keeps the exact authored bytes (append-only, never
    rewritten). The presented content is that text with the system-maintained
    内容依据 appendix deterministically re-attached from
    ``evidence_encounter_ids``, so deleting the block in the editor cannot
    strip evidence provenance from display, copy, or export. ``summary=True``
    never TOUCHES content/structured output at all — list callers pass
    summary-projection rows where those columns are not even loaded from the
    database (正文与 AI 结构化输出按需在编辑页读取).
    """

    payload = {
        "id": str(row.id),
        "note_id": str(row.note_id),
        "patient_id": str(row.patient_id),
        "note_type": row.note_type.value,
        "version": row.version,
        "version_type": row.version_type.value,
        "title": row.title,
        "evidence_encounters": row.evidence_encounter_ids,
        "source_context_hash": row.source_context_hash,
        "evidence_snapshot": _evidence_snapshot_summary(row.evidence_snapshot),
        "provider": row.provider,
        "model": row.model,
        "prompt_version": row.prompt_version,
        "deidentification_version": row.deidentification_version,
        "based_on_version_id": (str(row.based_on_version_id) if row.based_on_version_id else None),
        "created_at": row.created_at,
        "source": (
            "ai_inferred" if row.version_type == NoteVersionType.AI_DRAFT else "student_note"
        ),
    }
    if summary:
        # 列表只投影摘要：正文与 AI 结构化输出不进响应，也不进列表查询。
        return payload
    presented = _strip_system_evidence_appendix(row.content)
    appendix = _evidence_appendix(db, row.evidence_encounter_ids, evidence_dates)
    if appendix:
        presented = f"{presented}\n\n{appendix}"
    payload["content"] = presented
    payload["structured_output"] = row.structured_output
    return payload


def _note_versions(db: Session, note_id: uuid.UUID) -> list[ApprenticeshipNoteVersion]:
    return list(
        db.scalars(
            select(ApprenticeshipNoteVersion)
            .where(ApprenticeshipNoteVersion.note_id == note_id)
            .order_by(ApprenticeshipNoteVersion.version.asc())
        ).all()
    )


def _latest_version(db: Session, note_id: uuid.UUID) -> ApprenticeshipNoteVersion:
    row = db.scalar(
        select(ApprenticeshipNoteVersion)
        .where(ApprenticeshipNoteVersion.note_id == note_id)
        .order_by(ApprenticeshipNoteVersion.version.desc())
        .limit(1)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return row


def _build_evidence_snapshot(
    note_type: ApprenticeshipNoteType,
    patient: Patient,
    encounters: list[Encounter],
    direct_identifiers: list[str],
) -> dict[str, Any]:
    """依据快照：把该作品版本实际采用的写作材料整体固化。

    快照就是去标识的写作上下文全文（就诊范围与顺序、事实、学生记录、
    Phase 7 确定性对比结果）加上各层版本号与上下文哈希——重建当时材料
    所需的一切，且不携带任何身份信息。旧版本没有快照就保持 NULL，
    如实标记，绝不按当前资料补成历史快照。
    """

    builder = WritingContextBuilder()
    if note_type == ApprenticeshipNoteType.TEACHER_NOTE:
        ordered = _ordered_encounters(patient)
        target = encounters[0]
        index = ordered.index(target)
        previous = ordered[index - 1] if index > 0 else None
        context: TeacherNoteContext | CaseRecordContext = builder.build_teacher_note_context(
            patient, target, previous, direct_identifiers
        )
    else:
        context = builder.build_case_record_context(patient, encounters, direct_identifiers)
    context_payload = context.model_dump(mode="json")
    return {
        "set_version": EVIDENCE_SNAPSHOT_SET_VERSION,
        "context_version": WRITING_CONTEXT_VERSION,
        "prescription_diff_algorithm_version": PRESCRIPTION_DIFF_ALGORITHM_VERSION,
        "deidentification_version": DEIDENTIFICATION_VERSION,
        "context_hash": context_hash(context_payload),
        "context": context_payload,
    }


def _context_hash_for_note(
    note_type: ApprenticeshipNoteType,
    patient: Patient,
    encounters: list[Encounter],
    direct_identifiers: list[str],
) -> str:
    return _build_evidence_snapshot(note_type, patient, encounters, direct_identifiers)[
        "context_hash"
    ]


def _append_version(
    db: Session,
    *,
    patient_id: uuid.UUID,
    note_id: uuid.UUID,
    note_type: ApprenticeshipNoteType,
    version: int,
    version_type: NoteVersionType,
    content: str,
    evidence_encounter_ids: list[str],
    source_context_hash: str,
    structured_output: dict[str, Any] | None,
    provider: str | None,
    model: str | None,
    prompt_version: int | None,
    based_on_version_id: uuid.UUID | None,
    evidence_snapshot: dict[str, Any] | None = None,
    row_id: uuid.UUID | None = None,
) -> ApprenticeshipNoteVersion:
    row = ApprenticeshipNoteVersion(
        id=row_id or uuid.uuid4(),
        note_id=note_id,
        patient_id=patient_id,
        note_type=note_type,
        version=version,
        version_type=version_type,
        content=content,
        evidence_encounter_ids=evidence_encounter_ids,
        source_context_hash=source_context_hash,
        evidence_snapshot=evidence_snapshot,
        structured_output=structured_output,
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        deidentification_version=DEIDENTIFICATION_VERSION,
        based_on_version_id=based_on_version_id,
    )
    db.add(row)
    db.flush()
    return row


_VERSION_CONFLICT_RETRIES = 3


def _next_note_version(db: Session, note_id: uuid.UUID) -> int:
    current = db.scalar(
        select(func.max(ApprenticeshipNoteVersion.version)).where(
            ApprenticeshipNoteVersion.note_id == note_id
        )
    )
    return (current or 0) + 1


def _append_version_resolving_conflicts(
    db: Session,
    *,
    note_id: uuid.UUID,
    build_version_row: Callable[[int], ApprenticeshipNoteVersion],
) -> ApprenticeshipNoteVersion:
    """Append one version, retrying when another window claimed the number.

    "Read latest version, then insert latest + 1" is not atomic across
    windows or double submissions; the ``(note_id, version)`` unique
    constraint catches the race and this loop re-reads the fresh maximum and
    rebuilds the row. A conflict aborts the transaction, so
    ``build_version_row`` must recreate every write the row depends on (the
    prompt-template upsert included). The chain stays append-only: retries
    allocate a new number and never reuse or overwrite an existing version.
    """

    for _ in range(_VERSION_CONFLICT_RETRIES):
        try:
            return build_version_row(_next_note_version(db, note_id))
        except IntegrityError:
            db.rollback()
    raise HTTPException(
        status_code=409,
        detail="保存冲突：另一窗口刚写入了新版本，请重试",
    )


def _generate_ai_draft_version(
    db: Session,
    *,
    patient: Patient,
    note_type: ApprenticeshipNoteType,
    encounters: list[Encounter],
    note_id: uuid.UUID,
    based_on_version_id: uuid.UUID | None,
) -> ApprenticeshipNoteVersion:
    direct_identifiers = _direct_identifiers(patient)
    # Fail closed before any prompt-template rows are written.
    if get_apprenticeship_writing_provider(db) is None:
        raise HTTPException(
            status_code=503,
            detail="请先在设置中配置并启用“师承写作 Provider”",
        )
    builder = WritingContextBuilder()
    if note_type == ApprenticeshipNoteType.TEACHER_NOTE:
        ordered = _ordered_encounters(patient)
        target = encounters[0]
        index = ordered.index(target)
        previous = ordered[index - 1] if index > 0 else None
        try:
            context: TeacherNoteContext | CaseRecordContext = builder.build_teacher_note_context(
                patient, target, previous, direct_identifiers
            )
        except AmbiguousPrescriptionDiffError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    "PRESCRIPTION_DIFF_AMBIGUOUS：该就诊与前诊存在无法唯一配对的处方，"
                    "跟师笔记已停止生成，未让 AI 猜测"
                ),
            ) from exc
        prompt_task_type = AITaskType.TEACHER_NOTE_GENERATION
        prompt_version_number = TEACHER_NOTE_PROMPT_VERSION
        prompt_text = TEACHER_NOTE_SYSTEM_PROMPT
        prompt_output_schema: dict[str, Any] = TeacherNoteDraft.model_json_schema()
    else:
        try:
            context = builder.build_case_record_context(patient, encounters, direct_identifiers)
        except AmbiguousPrescriptionDiffError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    "PRESCRIPTION_DIFF_AMBIGUOUS：所选范围存在无法唯一配对的处方，"
                    "医案整理已停止生成，未让 AI 猜测"
                ),
            ) from exc
        prompt_task_type = AITaskType.CASE_RECORD_GENERATION
        prompt_version_number = CASE_RECORD_PROMPT_VERSION
        prompt_text = CASE_RECORD_SYSTEM_PROMPT
        prompt_output_schema = CaseRecordDraft.model_json_schema()

    provider = get_apprenticeship_writing_provider(db)
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="请先在设置中配置并启用“师承写作 Provider”",
        )
    prompt_row = _writing_prompt_version(
        db,
        task_type=prompt_task_type,
        version_number=prompt_version_number,
        prompt_text=prompt_text,
        output_schema=prompt_output_schema,
    )
    context_payload = context.model_dump(mode="json")
    try:
        provider_result = provider.run(
            prompt=prompt_row.prompt_text,
            context=context_payload,
            schema=prompt_row.output_schema,
        )
        if isinstance(context, TeacherNoteContext):
            teacher_draft = validate_teacher_note_output(provider_result.structured_output, context)
            rendered = render_teacher_note(teacher_draft, context)
            structured_output_payload = teacher_draft.model_dump(mode="json")
        else:
            case_draft = validate_case_record_output(provider_result.structured_output, context)
            rendered = render_case_record(case_draft, context)
            structured_output_payload = case_draft.model_dump(mode="json")
    except AIProviderError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except WritingRejected as exc:
        db.rollback()
        raise HTTPException(
            status_code=502,
            detail="AI 输出未通过结构或证据校验，未保存本次草稿",
        ) from exc

    evidence_encounter_ids = sorted({str(encounter.id) for encounter in encounters})
    evidence_snapshot: dict[str, Any] = {
        "set_version": EVIDENCE_SNAPSHOT_SET_VERSION,
        "context_version": WRITING_CONTEXT_VERSION,
        "prescription_diff_algorithm_version": PRESCRIPTION_DIFF_ALGORITHM_VERSION,
        "deidentification_version": DEIDENTIFICATION_VERSION,
        "context_hash": context_hash(context_payload),
        "context": context_payload,
    }

    def build_version_row(version: int) -> ApprenticeshipNoteVersion:
        # A conflict rollback discards the prompt-template upsert made before
        # the provider call, so it repeats inside the retry scope; it is an
        # upsert and resolves to the identical row.
        prompt_row = _writing_prompt_version(
            db,
            task_type=prompt_task_type,
            version_number=prompt_version_number,
            prompt_text=prompt_text,
            output_schema=prompt_output_schema,
        )
        return _append_version(
            db,
            patient_id=patient.id,
            note_id=note_id,
            note_type=note_type,
            version=version,
            version_type=NoteVersionType.AI_DRAFT,
            content=rendered,
            evidence_encounter_ids=evidence_encounter_ids,
            source_context_hash=evidence_snapshot["context_hash"],
            evidence_snapshot=evidence_snapshot,
            structured_output=structured_output_payload,
            provider=provider_result.provider,
            model=provider_result.model,
            prompt_version=prompt_row.version,
            based_on_version_id=based_on_version_id,
        )

    # The provider call above happened exactly once; only the append retries.
    return _append_version_resolving_conflicts(
        db,
        note_id=note_id,
        build_version_row=build_version_row,
    )


@router.post("/encounters/{encounter_id}/teacher-note-drafts", status_code=201)
def generate_teacher_note_draft(
    encounter_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    encounter = db.scalar(select(Encounter).where(Encounter.id == encounter_id))
    if encounter is None:
        raise HTTPException(status_code=404, detail="Encounter not found")
    patient = _patient_with_writing_context(db, encounter.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    target = _require_encounter_in_patient(patient, encounter.id)

    row = _generate_ai_draft_version(
        db,
        patient=patient,
        note_type=ApprenticeshipNoteType.TEACHER_NOTE,
        encounters=[target],
        note_id=uuid.uuid4(),
        based_on_version_id=None,
    )
    append_audit_event(
        db,
        action="teacher_note_draft.created",
        entity_type="apprenticeship_note_version",
        entity_id=row.id,
        patient_id=patient.id,
        actor_type="user",
        after_redacted={
            "note_id": str(row.note_id),
            "version": row.version,
            "provider": row.provider,
            "model": row.model,
            "prompt_version": row.prompt_version,
            "source_context_hash": row.source_context_hash,
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize_version(db, row)


@router.post("/patients/{patient_id}/case-record-drafts", status_code=201)
def generate_case_record_draft(
    patient_id: uuid.UUID,
    payload: CaseRecordGenerateRequest,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    patient = _patient_with_writing_context(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    encounters = _select_encounter_range(
        patient, payload.start_encounter_id, payload.end_encounter_id
    )
    if not encounters:
        raise HTTPException(status_code=422, detail="该患者暂无可整理的就诊记录")

    row = _generate_ai_draft_version(
        db,
        patient=patient,
        note_type=ApprenticeshipNoteType.CASE_RECORD,
        encounters=encounters,
        note_id=uuid.uuid4(),
        based_on_version_id=None,
    )
    append_audit_event(
        db,
        action="case_record_draft.created",
        entity_type="apprenticeship_note_version",
        entity_id=row.id,
        patient_id=patient.id,
        actor_type="user",
        after_redacted={
            "note_id": str(row.note_id),
            "version": row.version,
            "encounter_count": len(encounters),
            "provider": row.provider,
            "model": row.model,
            "prompt_version": row.prompt_version,
            "source_context_hash": row.source_context_hash,
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize_version(db, row)


@router.post("/notes/{note_id}/regenerate", status_code=201)
def regenerate_note_draft(
    note_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    latest = _latest_version(db, note_id)
    patient = _patient_with_writing_context(db, latest.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    evidence_ids = [uuid.UUID(value) for value in latest.evidence_encounter_ids]
    encounters = [_require_encounter_in_patient(patient, value) for value in evidence_ids]

    row = _generate_ai_draft_version(
        db,
        patient=patient,
        note_type=latest.note_type,
        encounters=encounters,
        note_id=latest.note_id,
        based_on_version_id=latest.id,
    )
    append_audit_event(
        db,
        action="note_draft.regenerated",
        entity_type="apprenticeship_note_version",
        entity_id=row.id,
        patient_id=patient.id,
        actor_type="user",
        after_redacted={
            "note_id": str(row.note_id),
            "version": row.version,
            "based_on_version": latest.version,
            "provider": row.provider,
            "model": row.model,
            "prompt_version": row.prompt_version,
            "source_context_hash": row.source_context_hash,
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize_version(db, row)


@router.post("/notes/{note_id}/revisions", status_code=201)
def create_note_revision(
    note_id: uuid.UUID,
    payload: NoteRevisionCreate,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    if payload.request_id and payload.based_on_version_id is None:
        raise HTTPException(status_code=422, detail="可重试保存必须指定所基于的版本")
    requested_evidence_ids = (
        sorted({str(value) for value in payload.evidence_encounter_ids})
        if payload.evidence_encounter_ids is not None
        else None
    )

    def replay() -> ApprenticeshipNoteVersion | None:
        existing = (
            db.get(ApprenticeshipNoteVersion, payload.request_id) if payload.request_id else None
        )
        if existing is not None and (
            existing.note_id != note_id
            or existing.content != payload.content
            or existing.based_on_version_id != payload.based_on_version_id
            or existing.version_type
            != (NoteVersionType.USER_FINAL if payload.final else NoteVersionType.USER_DRAFT)
            or (
                requested_evidence_ids is not None
                and sorted(existing.evidence_encounter_ids) != requested_evidence_ids
            )
        ):
            raise HTTPException(status_code=409, detail="保存标识已用于其他内容，请核对后重新提交")
        return existing

    existing = replay()
    if existing is not None:
        return _serialize_version(db, existing)
    latest = _latest_version(db, note_id)
    based_on = latest
    if payload.based_on_version_id is not None:
        candidate = db.scalar(
            select(ApprenticeshipNoteVersion).where(
                ApprenticeshipNoteVersion.id == payload.based_on_version_id,
                ApprenticeshipNoteVersion.note_id == note_id,
            )
        )
        if candidate is None:
            raise HTTPException(status_code=422, detail="所基于的版本不属于该笔记")
        based_on = candidate

    # 版本依据一致性：一次修订永远继承“所基于版本”的证据集合与上下文哈希，
    # 不取最新版本、不按当前数据库重算。编辑旧稿不会静默换到新依据；
    # 要采用新资料必须走重新生成（显式重算并追加版本）。
    # 依据快照：修改文字时逐字继承所基于版本的快照（含其哈希），历史材料
    # 原样随行；快照为 NULL 的历史版本保持 NULL，绝不按当前资料回填。
    replayed = False

    evidence_ids: list[str]
    adopted_snapshot: dict[str, Any] | None
    # 正文引用计数（确定性，只留痕不阻断）：采用新依据时统计“本次移除的
    # 就诊”在正文（剥离系统维护的依据附录、大小写不敏感）中的出现。口径
    # 有意窄于界面提示——界面还报告“未识别标识”等其他 UUID，两边数字不必
    # 相等；审计只回答“被移除的就诊是否仍被正文引用”。正文永远不会被
    # 自动改写，普通叙述是否仍然成立由用户核对。
    removed_encounter_reference_count = 0
    if payload.evidence_encounter_ids is None:
        evidence_ids = list(based_on.evidence_encounter_ids)
        adopted_snapshot = based_on.evidence_snapshot
        source_context_hash = based_on.source_context_hash
    else:
        # 采用新资料整理：显式的新依据集合，按当前数据库重算快照与哈希。
        # 跟师笔记仍然只允许一次就诊；快照构建命中歧义处方对比即整体失败
        # （fail closed），原依据保持不变。
        patient = _patient_with_writing_context(db, based_on.patient_id)
        if patient is None:
            raise HTTPException(status_code=404, detail="Patient not found")
        # 与预览端点同一套规范化：排序去重后解析，快照与入库集合永远一致。
        unique_ids = requested_evidence_ids or []
        encounters = [
            _require_encounter_in_patient(patient, uuid.UUID(value)) for value in unique_ids
        ]
        if based_on.note_type == ApprenticeshipNoteType.TEACHER_NOTE and len(encounters) != 1:
            raise HTTPException(status_code=422, detail="跟师笔记必须且只能关联一次就诊")
        try:
            adopted_snapshot = _build_evidence_snapshot(
                based_on.note_type, patient, encounters, _direct_identifiers(patient)
            )
        except AmbiguousPrescriptionDiffError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    "PRESCRIPTION_DIFF_AMBIGUOUS：所选就诊范围存在无法唯一配对的处方，"
                    "采用新依据已停止，原依据保持不变"
                ),
            ) from exc
        # 预览凭证校验：采用前重算“此刻的快照会得到什么凭证”，与请求携带
        # 的凭证比对。材料在预览后变化、或预览与本次采用范围/版本不一致，
        # 一律拒绝并要求重新预览——用户确认的快照必须就是入库的快照。
        # （按请求标识命中的历史重放在更早处直接返回，不会走到这里。）
        if not payload.preview_token:
            raise HTTPException(
                status_code=422,
                detail="缺少预览凭证：请先预览拟采用的变化并确认后重试",
            )
        expected_token = _evidence_preview_token(
            note_id, based_on.id, unique_ids, adopted_snapshot
        )
        if payload.preview_token != expected_token:
            raise HTTPException(
                status_code=409,
                detail=(
                    "所选资料在预览后已发生变化（或预览与本次采用不匹配）："
                    "请重新预览并确认后再采用；原依据保持不变"
                ),
            )
        evidence_ids = unique_ids
        source_context_hash = adopted_snapshot["context_hash"]
        removed_ids = sorted(set(based_on.evidence_encounter_ids) - set(unique_ids))
        body_text = _strip_system_evidence_appendix(payload.content).lower()
        removed_encounter_reference_count = sum(
            1 for value in removed_ids if value.lower() in body_text
        )

    def build_version_row(version: int) -> ApprenticeshipNoteVersion:
        nonlocal replayed
        existing = replay()
        if existing is not None:
            replayed = True
            return existing
        return _append_version(
            db,
            row_id=payload.request_id,
            patient_id=based_on.patient_id,
            note_id=note_id,
            note_type=based_on.note_type,
            version=version,
            version_type=(
                NoteVersionType.USER_FINAL if payload.final else NoteVersionType.USER_DRAFT
            ),
            content=payload.content,
            evidence_encounter_ids=evidence_ids,
            source_context_hash=source_context_hash,
            evidence_snapshot=adopted_snapshot,
            structured_output=None,
            provider=None,
            model=None,
            prompt_version=None,
            based_on_version_id=based_on.id,
        )

    row = _append_version_resolving_conflicts(
        db,
        note_id=note_id,
        build_version_row=build_version_row,
    )
    if replayed:
        return _serialize_version(db, row)
    append_audit_event(
        db,
        action="note_version.revised",
        entity_type="apprenticeship_note_version",
        entity_id=row.id,
        patient_id=latest.patient_id,
        actor_type="user",
        after_redacted={
            "note_id": str(note_id),
            "version": row.version,
            "version_type": row.version_type.value,
            "based_on_version": based_on.version,
            "content_length": len(payload.content),
            "removed_encounter_reference_count": removed_encounter_reference_count,
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize_version(db, row)


def _materials_breakdown(
    based_on: ApprenticeshipNoteVersion, patient: Patient | None
) -> dict[str, Any]:
    """把“材料变化”从单一哈希差异拆成可解释的三类。

    - ``kept_materials_changed``：按当前规则重算“原依据范围”，与该版本
      存储快照的哈希对比——回答“已有资料（就诊内容、补录）是否变了”。
    - ``rule_versions_changed``：存储快照各层版本号与当前常量的差异——
      规则变了会让哈希不可直接互比，必须单独呈现，不能混进“资料变化”。
    - 范围增减由 proposal.added/removed 表达，不在此重复。

    不可比较的情形如实降级并说明原因，绝不按当前资料回填历史快照。
    """

    stored = based_on.evidence_snapshot
    if stored is None:
        return {
            "comparable": False,
            "not_comparable_reason": "legacy_no_snapshot",
            "kept_materials_changed": False,
            "rule_versions_changed": {},
        }
    current_versions = {
        "set_version": EVIDENCE_SNAPSHOT_SET_VERSION,
        "context_version": WRITING_CONTEXT_VERSION,
        "prescription_diff_algorithm_version": PRESCRIPTION_DIFF_ALGORITHM_VERSION,
        "deidentification_version": DEIDENTIFICATION_VERSION,
    }
    rule_versions_changed = {
        key: {"stored": stored.get(key), "current": value}
        for key, value in current_versions.items()
        if stored.get(key) != value
    }
    base_ids = [str(value) for value in based_on.evidence_encounter_ids if value]
    if patient is None or not base_ids or (
        based_on.note_type == ApprenticeshipNoteType.TEACHER_NOTE and len(base_ids) != 1
    ):
        return {
            "comparable": False,
            "not_comparable_reason": "base_set_unavailable",
            "kept_materials_changed": False,
            "rule_versions_changed": rule_versions_changed,
        }
    try:
        base_encounters = [
            _require_encounter_in_patient(patient, uuid.UUID(value)) for value in base_ids
        ]
        base_snapshot = _build_evidence_snapshot(
            based_on.note_type, patient, base_encounters, _direct_identifiers(patient)
        )
    except HTTPException:
        return {
            "comparable": False,
            "not_comparable_reason": "base_set_unavailable",
            "kept_materials_changed": False,
            "rule_versions_changed": rule_versions_changed,
        }
    except AmbiguousPrescriptionDiffError:
        return {
            "comparable": False,
            "not_comparable_reason": "base_ambiguous",
            "kept_materials_changed": False,
            "rule_versions_changed": rule_versions_changed,
        }
    return {
        "comparable": True,
        "not_comparable_reason": None,
        "kept_materials_changed": (
            base_snapshot["context_hash"] != stored.get("context_hash")
        ),
        "rule_versions_changed": rule_versions_changed,
    }


@router.post("/notes/{note_id}/evidence-preview")
def preview_note_evidence(
    note_id: uuid.UUID,
    payload: EvidencePreviewRequest,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    """预览“采用所选资料”会得到什么——只读，绝不落库。

    预览绑定 ``based_on_version_id``：用户正在查看哪个版本，增减数量就按
    哪个版本的依据计算——绝不偷偷换成最新版本（采用按所选版本执行，预览
    必须对同一版本承诺）。响应携带 ``preview_token``（对版本、规范化就诊
    集合、材料哈希与规则版本的整体哈希），采用请求必须原样带回；服务端按
    当前数据库重算比对，期间材料变化即拒绝。Reports the encounter-set diff
    (added/removed, chronological patient list for the picker included) and
    whether recomputing the materials for the proposed set would change them.
    Ambiguous prescription diffs fail closed with the SAME 409 the adoption
    path raises, so the preview never promises anything adoption cannot do;
    the stored version and its evidence stay untouched.
    """

    latest = _latest_version(db, note_id)
    based_on = latest
    if payload.based_on_version_id != latest.id:
        candidate = db.scalar(
            select(ApprenticeshipNoteVersion).where(
                ApprenticeshipNoteVersion.id == payload.based_on_version_id,
                ApprenticeshipNoteVersion.note_id == note_id,
            )
        )
        if candidate is None:
            raise HTTPException(status_code=422, detail="预览所基于的版本不属于该笔记")
        based_on = candidate
    patient = _patient_with_writing_context(db, based_on.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    # 与采用路径同一套规范化：排序去重后解析，重复勾选不会算两次材料。
    proposed_ids = sorted({str(value) for value in payload.evidence_encounter_ids})
    encounters = [
        _require_encounter_in_patient(patient, uuid.UUID(value)) for value in proposed_ids
    ]
    if based_on.note_type == ApprenticeshipNoteType.TEACHER_NOTE and len(encounters) != 1:
        raise HTTPException(status_code=422, detail="跟师笔记必须且只能关联一次就诊")
    try:
        snapshot = _build_evidence_snapshot(
            based_on.note_type, patient, encounters, _direct_identifiers(patient)
        )
    except AmbiguousPrescriptionDiffError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "PRESCRIPTION_DIFF_AMBIGUOUS：所选就诊范围存在无法唯一配对的处方，"
                "无法预览；原依据保持不变"
            ),
        ) from exc

    current_ids = [str(value) for value in based_on.evidence_encounter_ids]
    added = sorted(set(proposed_ids) - set(current_ids))
    removed = sorted(set(current_ids) - set(proposed_ids))
    return {
        "note_id": str(note_id),
        "note_type": based_on.note_type.value,
        "based_on_version": {
            "id": str(based_on.id),
            "version": based_on.version,
            "version_type": based_on.version_type.value,
        },
        "latest_version": {"id": str(latest.id), "version": latest.version},
        "current": {
            "evidence_encounters": current_ids,
            "source_context_hash": based_on.source_context_hash,
            "evidence_snapshot": _evidence_snapshot_summary(based_on.evidence_snapshot),
        },
        "patient_encounters": _patient_encounter_picker_rows(db, based_on.patient_id),
        "proposal": {
            "evidence_encounters": proposed_ids,
            "added": added,
            "removed": removed,
            "source_context_hash": snapshot["context_hash"],
            "evidence_snapshot": _evidence_snapshot_summary(snapshot),
            "preview_token": _evidence_preview_token(note_id, based_on.id, proposed_ids, snapshot),
        },
        "set_changed": bool(added or removed),
        "materials_changed": snapshot["context_hash"] != based_on.source_context_hash,
        "materials_breakdown": _materials_breakdown(based_on, patient),
    }


@router.post("/patients/{patient_id}/notes", status_code=201)
def create_user_original_note(
    patient_id: uuid.UUID,
    payload: UserOriginalNoteCreate,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    patient = _patient_with_writing_context(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    if payload.note_type == ApprenticeshipNoteType.TEACHER_NOTE and len(payload.encounter_ids) != 1:
        raise HTTPException(status_code=422, detail="跟师笔记必须且只能关联一次就诊")
    encounters = [_require_encounter_in_patient(patient, value) for value in payload.encounter_ids]
    evidence_ids = sorted({str(encounter.id) for encounter in encounters})
    snapshot = _build_evidence_snapshot(
        payload.note_type, patient, encounters, _direct_identifiers(patient)
    )
    row = _append_version(
        db,
        patient_id=patient.id,
        note_id=uuid.uuid4(),
        note_type=payload.note_type,
        version=1,
        version_type=NoteVersionType.USER_ORIGINAL,
        content=payload.content,
        evidence_encounter_ids=evidence_ids,
        source_context_hash=snapshot["context_hash"],
        evidence_snapshot=snapshot,
        structured_output=None,
        provider=None,
        model=None,
        prompt_version=None,
        based_on_version_id=None,
    )
    append_audit_event(
        db,
        action="note_user_original.created",
        entity_type="apprenticeship_note_version",
        entity_id=row.id,
        patient_id=patient.id,
        actor_type="user",
        after_redacted={
            "note_id": str(row.note_id),
            "note_type": row.note_type.value,
            "content_length": len(payload.content),
        },
    )
    db.commit()
    db.refresh(row)
    return _serialize_version(db, row)


@router.get("/patients/{patient_id}/notes")
def list_patient_notes(
    patient_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    limit: int = 200,
    offset: int = 0,
    sort: str = "first_created",
    note_type: ApprenticeshipNoteType | None = None,
    encounter_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """该患者的师承作品列表：数据库侧过滤/排序/分页 + 摘要投影。

    条目语义与历史实现一致（latest 为最新版本，latest_final 为最新定稿，
    version_count 为该作品全部版本数——过滤只决定哪些作品入选，不改变计数）。
    分页/过滤全部在数据库内完成，页面行使用受限列投影——正文、AI 结构化输出
    与全量历史版本不进入列表查询（P1-B 计时驱动的改动，见
    Codex/outputs/perf_baseline_20260910.md）。

    - ``sort=first_created``（默认）：按作品首个版本创建时间升序（原语义）；
      ``sort=latest``：按最新版本创建时间降序（新建/刚更新的作品排最前）。
    - ``note_type`` / ``encounter_id``：按**最新版本**的类型与依据就诊过滤
      （version_count/latest_final 仍统计该作品全部版本）。
    """
    if db.get(Patient, patient_id) is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    if sort not in {"first_created", "latest"}:
        raise HTTPException(status_code=422, detail="sort 仅支持 first_created 或 latest")
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    per_note = (
        select(
            ApprenticeshipNoteVersion.note_id,
            func.max(ApprenticeshipNoteVersion.version).label("max_version"),
            func.count(ApprenticeshipNoteVersion.id).label("version_count"),
            func.min(ApprenticeshipNoteVersion.created_at).label("first_created_at"),
            func.max(ApprenticeshipNoteVersion.created_at).label("last_created_at"),
        )
        .where(ApprenticeshipNoteVersion.patient_id == patient_id)
        .group_by(ApprenticeshipNoteVersion.note_id)
        .subquery()
    )
    # 每个作品的最新版本行：过滤条件（类型/依据就诊）作用在最新版本上，
    # 但 version_count / latest_final 仍基于该作品的全部版本。
    latest_rows = (
        select(ApprenticeshipNoteVersion)
        .join(
            per_note,
            and_(
                ApprenticeshipNoteVersion.note_id == per_note.c.note_id,
                ApprenticeshipNoteVersion.version == per_note.c.max_version,
            ),
        )
        .subquery()
    )
    base = select(per_note).join(latest_rows, latest_rows.c.note_id == per_note.c.note_id)
    if note_type is not None:
        base = base.where(latest_rows.c.note_type == note_type)
    if encounter_id is not None:
        # evidence_encounter_ids 是 JSON 数组：文本包含该就诊 id 即视为关联
        # （uuid 字符串格式固定，不存在歧义子串）。
        base = base.where(
            latest_rows.c.evidence_encounter_ids.cast(String).like(f"%{encounter_id}%")
        )
    total = db.scalar(select(func.count()).select_from(base.subquery()))
    if sort == "latest":
        ordering = (per_note.c.last_created_at.desc(), per_note.c.note_id.desc())
    else:
        ordering = (per_note.c.first_created_at.asc(), per_note.c.note_id.asc())
    page = db.execute(base.order_by(*ordering).offset(offset).limit(limit)).all()
    page_meta = {row.note_id: row for row in page}
    page_ids = [row.note_id for row in page]

    # 页面行的受限列投影（与全局 /notes 列表同一套列集合）。
    summary_load = load_only(
        ApprenticeshipNoteVersion.id,
        ApprenticeshipNoteVersion.note_id,
        ApprenticeshipNoteVersion.patient_id,
        ApprenticeshipNoteVersion.note_type,
        ApprenticeshipNoteVersion.version,
        ApprenticeshipNoteVersion.version_type,
        ApprenticeshipNoteVersion.title,
        ApprenticeshipNoteVersion.evidence_encounter_ids,
        ApprenticeshipNoteVersion.source_context_hash,
        ApprenticeshipNoteVersion.evidence_snapshot,
        ApprenticeshipNoteVersion.provider,
        ApprenticeshipNoteVersion.model,
        ApprenticeshipNoteVersion.prompt_version,
        ApprenticeshipNoteVersion.deidentification_version,
        ApprenticeshipNoteVersion.based_on_version_id,
        ApprenticeshipNoteVersion.created_at,
    )
    latest_join = and_(
        ApprenticeshipNoteVersion.note_id == per_note.c.note_id,
        ApprenticeshipNoteVersion.version == per_note.c.max_version,
    )
    final_pairs = (
        select(
            ApprenticeshipNoteVersion.note_id,
            func.max(ApprenticeshipNoteVersion.version).label("max_version"),
        )
        .where(
            ApprenticeshipNoteVersion.patient_id == patient_id,
            ApprenticeshipNoteVersion.version_type == NoteVersionType.USER_FINAL,
            ApprenticeshipNoteVersion.note_id.in_(page_ids) if page_ids else true(),
        )
        .group_by(ApprenticeshipNoteVersion.note_id)
        .subquery()
    )
    final_join = and_(
        ApprenticeshipNoteVersion.note_id == final_pairs.c.note_id,
        ApprenticeshipNoteVersion.version == final_pairs.c.max_version,
    )
    latest_row: dict[uuid.UUID, ApprenticeshipNoteVersion] = {}
    final_row: dict[uuid.UUID, ApprenticeshipNoteVersion] = {}
    if page_ids:
        for row in db.scalars(
            select(ApprenticeshipNoteVersion)
            .join(per_note, latest_join)
            .where(ApprenticeshipNoteVersion.note_id.in_(page_ids))
            .options(summary_load)
        ).all():
            latest_row[row.note_id] = row
        for row in db.scalars(
            select(ApprenticeshipNoteVersion)
            .join(final_pairs, final_join)
            .options(summary_load)
        ).all():
            final_row[row.note_id] = row

    evidence_ids = [
        value
        for row in [*latest_row.values(), *final_row.values()]
        for value in row.evidence_encounter_ids
    ]
    evidence_dates = _encounter_date_map(db, evidence_ids)
    encounter_rows = _patient_encounter_rows(db, {patient_id})

    items: list[dict[str, Any]] = []
    for note_id in page_ids:
        row = latest_row.get(note_id)
        if row is None:
            continue
        meta = page_meta[note_id]
        note: dict[str, Any] = {
            "note_id": str(note_id),
            "patient_id": str(patient_id),
            "note_type": row.note_type.value,
            "version_count": meta.version_count,
            "first_created_at": meta.first_created_at,
            "latest": _serialize_version(db, row, evidence_dates, summary=True),
            "latest_final": None,
            "evidence_window": _evidence_window(
                encounter_rows.get(patient_id, []),
                row.evidence_encounter_ids,
                evidence_dates,
            ),
        }
        final = final_row.get(note_id)
        if final is not None:
            note["latest_final"] = _serialize_version(db, final, evidence_dates, summary=True)
        items.append(note)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/notes")
def list_all_notes(
    user: CurrentUser,
    db: DbSession,
    limit: int = 50,
    offset: int = 0,
    note_type: ApprenticeshipNoteType | None = None,
    patient_id: uuid.UUID | None = None,
    finalized: bool | None = None,
) -> dict[str, Any]:
    """Global writing library: every note with its latest edit AND latest final.

    ``latest`` is the newest version regardless of authorship layer;
    ``latest_final`` is the newest ``user_final`` version (null when the note
    was never finalized). Returning both keeps a previously finalized note
    visible after a new draft is appended on top of it.

    Filtering, ordering, paging and counting all happen INSIDE the database
    over the same restricted set; page rows use a summary projection so
    ``content`` and ``structured_output`` never enter a list query at all —
    the library stays light as the corpus grows (bodies load on demand in
    the editor). The latest-final lookup and per-note version counts are
    scoped to the current page's notes, not the whole corpus.
    """

    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    # 列表摘要投影：正文与 AI 结构化输出永不进入列表查询。
    summary_load = load_only(
        ApprenticeshipNoteVersion.id,
        ApprenticeshipNoteVersion.note_id,
        ApprenticeshipNoteVersion.patient_id,
        ApprenticeshipNoteVersion.note_type,
        ApprenticeshipNoteVersion.version,
        ApprenticeshipNoteVersion.version_type,
        ApprenticeshipNoteVersion.title,
        ApprenticeshipNoteVersion.evidence_encounter_ids,
        ApprenticeshipNoteVersion.source_context_hash,
        ApprenticeshipNoteVersion.evidence_snapshot,
        ApprenticeshipNoteVersion.provider,
        ApprenticeshipNoteVersion.model,
        ApprenticeshipNoteVersion.prompt_version,
        ApprenticeshipNoteVersion.deidentification_version,
        ApprenticeshipNoteVersion.based_on_version_id,
        ApprenticeshipNoteVersion.created_at,
    )

    latest_pairs = (
        select(
            ApprenticeshipNoteVersion.note_id,
            func.max(ApprenticeshipNoteVersion.version).label("max_version"),
        )
        .group_by(ApprenticeshipNoteVersion.note_id)
        .subquery()
    )
    latest_join = and_(
        ApprenticeshipNoteVersion.note_id == latest_pairs.c.note_id,
        ApprenticeshipNoteVersion.version == latest_pairs.c.max_version,
    )
    # 过滤器都作用于“最新版本”这一行：定稿筛选跟随 latest 的层级，
    # 与旧的 Python 侧过滤语义完全一致。
    conditions = []
    if note_type is not None:
        conditions.append(ApprenticeshipNoteVersion.note_type == note_type)
    if patient_id is not None:
        conditions.append(ApprenticeshipNoteVersion.patient_id == patient_id)
    if finalized is not None:
        conditions.append(
            ApprenticeshipNoteVersion.version_type == NoteVersionType.USER_FINAL
            if finalized
            else ApprenticeshipNoteVersion.version_type != NoteVersionType.USER_FINAL
        )

    total = db.scalar(
        select(func.count())
        .select_from(ApprenticeshipNoteVersion)
        .join(latest_pairs, latest_join)
        .where(*conditions)
    )
    latest_rows = list(
        db.scalars(
            select(ApprenticeshipNoteVersion)
            .join(latest_pairs, latest_join)
            .where(*conditions)
            .options(summary_load)
            .order_by(
                ApprenticeshipNoteVersion.created_at.desc(),
                ApprenticeshipNoteVersion.note_id.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
    )

    # 最新定稿版本与版本计数只查当前页涉及的作品：定稿后追加的新草稿不能
    # 让此前定稿从列表里消失，但其他页作品的定稿与本文无关。
    final_by_note: dict[uuid.UUID, ApprenticeshipNoteVersion] = {}
    counts: dict[uuid.UUID, tuple[int, Any]] = {}
    page_note_ids = [row.note_id for row in latest_rows]
    if page_note_ids:
        final_pairs = (
            select(
                ApprenticeshipNoteVersion.note_id,
                func.max(ApprenticeshipNoteVersion.version).label("max_version"),
            )
            .where(
                ApprenticeshipNoteVersion.version_type == NoteVersionType.USER_FINAL,
                ApprenticeshipNoteVersion.note_id.in_(page_note_ids),
            )
            .group_by(ApprenticeshipNoteVersion.note_id)
            .subquery()
        )
        final_by_note = {
            row.note_id: row
            for row in db.scalars(
                select(ApprenticeshipNoteVersion)
                .join(
                    final_pairs,
                    (ApprenticeshipNoteVersion.note_id == final_pairs.c.note_id)
                    & (ApprenticeshipNoteVersion.version == final_pairs.c.max_version),
                )
                .options(summary_load)
            ).all()
        }
        counts = {
            note_id: (version_count, first_created)
            for note_id, version_count, first_created in db.execute(
                select(
                    ApprenticeshipNoteVersion.note_id,
                    func.count(ApprenticeshipNoteVersion.id),
                    func.min(ApprenticeshipNoteVersion.created_at),
                )
                .where(ApprenticeshipNoteVersion.note_id.in_(page_note_ids))
                .group_by(ApprenticeshipNoteVersion.note_id)
            ).all()
        }

    evidence_dates = _encounter_date_map(
        db,
        (
            value
            for row in [*latest_rows, *final_by_note.values()]
            for value in row.evidence_encounter_ids
        ),
    )
    patient_rows: dict[uuid.UUID, Patient] = {}
    patient_ids = {row.patient_id for row in latest_rows}
    if patient_ids:
        patient_rows = {
            patient.id: patient
            for patient in db.scalars(
                select(Patient)
                .options(selectinload(Patient.identities))
                .where(Patient.id.in_(patient_ids))
            ).all()
        }
    encounter_rows = _patient_encounter_rows(db, patient_ids)

    items = []
    for row in latest_rows:
        final_row = final_by_note.get(row.note_id)
        version_count, first_created = counts.get(row.note_id, (1, row.created_at))
        patient = patient_rows.get(row.patient_id)
        items.append(
            {
                "note_id": str(row.note_id),
                "patient_id": str(row.patient_id),
                "patient": _patient_label(patient) if patient else None,
                "note_type": row.note_type.value,
                "version_count": version_count,
                "first_created_at": first_created,
                "latest": _serialize_version(db, row, evidence_dates, summary=True),
                "latest_final": (
                    _serialize_version(db, final_row, evidence_dates, summary=True)
                    if final_row is not None
                    else None
                ),
                "evidence_window": _evidence_window(
                    encounter_rows.get(row.patient_id, []),
                    row.evidence_encounter_ids,
                    evidence_dates,
                ),
            }
        )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _patient_label(patient: Patient) -> dict[str, Any]:
    identity = patient.identities[0] if patient.identities else None
    real_name = identity.real_name if identity else None
    return {
        "id": str(patient.id),
        "internal_patient_code": patient.internal_patient_code,
        "display_name": f"{real_name[0]}某" if real_name else None,
    }


@router.get("/notes/versions/{version_id}")
def get_note_version(
    version_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    """Resolve ONE version by id for save-result verification.

    A retryable save stores its ``request_id`` as the version row id, so the
    client answers “那次保存到底有没有入库？” with this lookup before deciding
    to retry: found -> the save landed (never retry); 404 -> safe to replay.
    """

    row = db.get(ApprenticeshipNoteVersion, version_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return _serialize_version(db, row)


@router.get("/notes/{note_id}")
def get_note_history(
    note_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    versions = _note_versions(db, note_id)
    if not versions:
        raise HTTPException(status_code=404, detail="Note not found")
    evidence_dates = _encounter_date_map(
        db,
        (value for version in versions for value in version.evidence_encounter_ids),
    )
    patient = db.scalar(
        select(Patient)
        .options(selectinload(Patient.identities))
        .where(Patient.id == versions[0].patient_id)
    )
    latest_version = versions[-1]
    evidence_window = _evidence_window(
        _patient_encounter_rows(db, {latest_version.patient_id}).get(latest_version.patient_id, []),
        latest_version.evidence_encounter_ids,
        evidence_dates,
    )
    return {
        "note_id": str(note_id),
        "patient_id": str(versions[0].patient_id),
        "patient": _patient_label(patient) if patient else None,
        "note_type": versions[0].note_type.value,
        "evidence_window": evidence_window,
        # 已采用的资料是否与当前数据一致（编辑器据此提示“需要重新核对”，
        # 绝不据此把历史作品判成错误或过期）。
        "evidence_status": _evidence_status_for_version(db, latest_version),
        "versions": [_serialize_version(db, row, evidence_dates) for row in versions],
    }
