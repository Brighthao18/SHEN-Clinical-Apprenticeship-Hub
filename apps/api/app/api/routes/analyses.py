from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.security import CurrentUser
from app.db.session import get_db
from app.models.domain import (
    AIAnalysis,
    AIAnalysisSourceDocument,
    Encounter,
    Patient,
    Prescription,
    PromptTemplate,
    PromptVersion,
)
from app.models.enums import AITaskType
from app.services.ai.context import DEIDENTIFICATION_VERSION, ClinicalContextBuilder
from app.services.ai.gateway import (
    AIProviderError,
    MockAIProvider,
    context_hash,
    get_longitudinal_analysis_provider,
)
from app.services.ai.longitudinal import (
    LONGITUDINAL_PROMPT_VERSION,
    LONGITUDINAL_SYSTEM_PROMPT,
    LongitudinalAnalysisRejected,
    LongitudinalContextBuilder,
    PatientLongitudinalAnalysis,
    validate_longitudinal_output,
)
from app.services.audit import append_audit_event
from app.services.prescription_diff import AmbiguousPrescriptionDiffError

router = APIRouter(tags=["analyses"])
DbSession = Annotated[Session, Depends(get_db)]


class AnalysisCreate(BaseModel):
    task_type: AITaskType = AITaskType.ENCOUNTER_INTERPRETATION


class LongitudinalAnalysisCreate(BaseModel):
    start_encounter_id: uuid.UUID | None = None
    end_encounter_id: uuid.UUID | None = None


def _patient_with_longitudinal_context(db: Session, patient_id: uuid.UUID) -> Patient | None:
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


def _select_encounter_range(
    patient: Patient, payload: LongitudinalAnalysisCreate
) -> list[Encounter]:
    ordered = sorted(
        patient.encounters, key=lambda item: (item.encounter_at, str(item.id))
    )
    if payload.start_encounter_id is None and payload.end_encounter_id is None:
        selected = ordered
    elif payload.start_encounter_id is None or payload.end_encounter_id is None:
        raise HTTPException(status_code=422, detail="开始和结束就诊必须同时选择")
    else:
        encounter_ids = [encounter.id for encounter in ordered]
        try:
            start_index = encounter_ids.index(payload.start_encounter_id)
            end_index = encounter_ids.index(payload.end_encounter_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="所选就诊不属于该患者") from exc
        if start_index > end_index:
            raise HTTPException(status_code=422, detail="开始就诊不能晚于结束就诊")
        selected = ordered[start_index : end_index + 1]
    if len(selected) < 2:
        raise HTTPException(status_code=422, detail="纵向分析至少需要两次就诊")
    return selected


def _longitudinal_prompt_version(db: Session) -> PromptVersion:
    task_type = AITaskType.PATIENT_LONGITUDINAL_ANALYSIS
    template = db.scalar(
        select(PromptTemplate).where(PromptTemplate.key == task_type.value)
    )
    if template is None:
        template = PromptTemplate(
            key=task_type.value,
            task_type=task_type,
            description="Phase 8 evidence-grounded patient longitudinal analysis",
        )
        db.add(template)
        db.flush()
    prompt_version = db.scalar(
        select(PromptVersion).where(
            PromptVersion.template_id == template.id,
            PromptVersion.version == LONGITUDINAL_PROMPT_VERSION,
        )
    )
    if prompt_version is None:
        prompt_version = PromptVersion(
            template_id=template.id,
            version=LONGITUDINAL_PROMPT_VERSION,
            prompt_text=LONGITUDINAL_SYSTEM_PROMPT,
            available_context={
                "ordered_encounters": True,
                "provenance": ["physician_record", "student_note", "ai_extracted"],
                "phase7_prescription_diff": "required_direct_input",
                "patient_identity": False,
            },
            retrieval_policy={
                "scope": "explicit_patient_encounter_range",
                "default": "all_encounters",
                "single_call": True,
                "rag": False,
            },
            output_schema=PatientLongitudinalAnalysis.model_json_schema(),
            status="active",
        )
        db.add(prompt_version)
        db.flush()
    return prompt_version


def _serialize_analysis(analysis: AIAnalysis) -> dict[str, Any]:
    return {
        "id": str(analysis.id),
        "task_type": analysis.task_type.value,
        "provider": analysis.provider,
        "model": analysis.model,
        "prompt_version_id": str(analysis.prompt_version_id),
        "created_at": analysis.created_at,
        "output": analysis.output,
        "structured_output": analysis.structured_output,
        "source_encounters": analysis.source_encounter_ids,
        "input_context_hash": analysis.input_context_hash,
        "deidentification_version": analysis.deidentification_version,
        "source": "ai_inferred",
    }


@router.post("/encounters/{encounter_id}/analyses")
def create_analysis(
    encounter_id: uuid.UUID,
    payload: AnalysisCreate,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    if payload.task_type != AITaskType.ENCOUNTER_INTERPRETATION:
        raise HTTPException(status_code=422, detail="Task is reserved but not enabled in V1")
    encounter = db.scalar(
        select(Encounter)
        .where(Encounter.id == encounter_id)
        .options(
            selectinload(Encounter.patient).selectinload(Patient.identities),
            selectinload(Encounter.symptoms),
            selectinload(Encounter.diagnoses),
            selectinload(Encounter.prescriptions).selectinload(Prescription.items),
        )
    )
    if encounter is None:
        raise HTTPException(status_code=404, detail="Encounter not found")

    direct_identifiers = _direct_identifiers(encounter.patient)
    context = ClinicalContextBuilder().build_encounter(encounter, direct_identifiers)

    template_key = payload.task_type.value
    template = db.scalar(select(PromptTemplate).where(PromptTemplate.key == template_key))
    if template is None:
        template = PromptTemplate(
            key=template_key,
            task_type=payload.task_type,
            description="V1 evidence-separated analysis prompt",
        )
        db.add(template)
        db.flush()
    prompt_version = db.scalar(
        select(PromptVersion).where(
            PromptVersion.template_id == template.id,
            PromptVersion.version == 1,
        )
    )
    if prompt_version is None:
        prompt_version = PromptVersion(
            template_id=template.id,
            version=1,
            prompt_text=(
                "Separate physician-recorded facts from hypotheses. "
                "Do not add missing clinical facts; "
                "cite source document IDs for each finding."
            ),
            available_context={"encounter": True, "patient_identity": False},
            retrieval_policy={"scope": "explicit_encounter_only"},
            output_schema={"type": "object", "required": ["findings", "uncertainties"]},
            status="active",
        )
        db.add(prompt_version)
        db.flush()

    provider_result = MockAIProvider().run(
        prompt=prompt_version.prompt_text,
        context=context,
        schema=prompt_version.output_schema,
    )
    analysis = AIAnalysis(
        patient_id=encounter.patient_id,
        encounter_id=encounter.id,
        task_type=payload.task_type,
        provider=provider_result.provider,
        model=provider_result.model,
        prompt_version_id=prompt_version.id,
        source_encounter_ids=[str(encounter.id)],
        source_chunk_ids=[],
        output=provider_result.output,
        structured_output=provider_result.structured_output,
        input_context_hash=context_hash(context),
        deidentification_version=DEIDENTIFICATION_VERSION,
        status="completed",
    )
    db.add(analysis)
    db.flush()
    db.add(
        AIAnalysisSourceDocument(
            analysis_id=analysis.id,
            document_id=encounter.primary_document_id,
        )
    )
    append_audit_event(
        db,
        action="encounter_analysis.created",
        entity_type="ai_analysis",
        entity_id=analysis.id,
        patient_id=encounter.patient_id,
        actor_type="user",
        after_redacted={
            "task_type": analysis.task_type.value,
            "provider": analysis.provider,
            "model": analysis.model,
            "encounter_id": str(encounter.id),
        },
    )
    db.commit()
    return {
        "id": str(analysis.id),
        "task_type": analysis.task_type.value,
        "provider": analysis.provider,
        "model": analysis.model,
        "prompt_version_id": str(prompt_version.id),
        "output": analysis.output,
        "structured_output": analysis.structured_output,
        "source_encounters": analysis.source_encounter_ids,
        "source_documents": [str(encounter.primary_document_id)],
        "deidentification_version": analysis.deidentification_version,
    }


@router.post("/patients/{patient_id}/longitudinal-analyses", status_code=201)
def create_longitudinal_analysis(
    patient_id: uuid.UUID,
    payload: LongitudinalAnalysisCreate,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    patient = _patient_with_longitudinal_context(db, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    encounters = _select_encounter_range(patient, payload)
    try:
        context = LongitudinalContextBuilder().build(
            patient,
            encounters,
            _direct_identifiers(patient),
            deidentification_version=DEIDENTIFICATION_VERSION,
        )
    except AmbiguousPrescriptionDiffError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "PRESCRIPTION_DIFF_AMBIGUOUS：所选范围存在无法唯一配对的处方，"
                "纵向分析已停止，未让 AI 猜测"
            ),
        ) from exc
    except LongitudinalAnalysisRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    provider = get_longitudinal_analysis_provider(db)
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="请先在设置中配置并启用“患者纵向分析 Provider”",
        )
    prompt_version = _longitudinal_prompt_version(db)
    context_payload = context.model_dump(mode="json")
    try:
        provider_result = provider.run(
            prompt=prompt_version.prompt_text,
            context=context_payload,
            schema=prompt_version.output_schema,
        )
        structured = validate_longitudinal_output(
            provider_result.structured_output, context
        )
    except AIProviderError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except LongitudinalAnalysisRejected as exc:
        db.rollback()
        raise HTTPException(
            status_code=502,
            detail="AI 输出未通过结构或证据校验，未保存本次结果",
        ) from exc

    analysis = AIAnalysis(
        patient_id=patient.id,
        encounter_id=None,
        task_type=AITaskType.PATIENT_LONGITUDINAL_ANALYSIS,
        provider=provider_result.provider,
        model=provider_result.model,
        prompt_version_id=prompt_version.id,
        source_encounter_ids=[str(encounter.id) for encounter in encounters],
        source_chunk_ids=[],
        output=provider_result.output,
        structured_output=structured.model_dump(mode="json"),
        input_context_hash=context_hash(context_payload),
        deidentification_version=DEIDENTIFICATION_VERSION,
        status="completed",
    )
    db.add(analysis)
    db.flush()
    for document_id in {encounter.primary_document_id for encounter in encounters}:
        db.add(
            AIAnalysisSourceDocument(
                analysis_id=analysis.id,
                document_id=document_id,
            )
        )
    append_audit_event(
        db,
        action="patient_longitudinal_analysis.created",
        entity_type="ai_analysis",
        entity_id=analysis.id,
        patient_id=patient.id,
        actor_type="user",
        after_redacted={
            "task_type": analysis.task_type.value,
            "provider": analysis.provider,
            "model": analysis.model,
            "prompt_version": LONGITUDINAL_PROMPT_VERSION,
            "source_encounter_count": len(encounters),
            "input_context_hash": analysis.input_context_hash,
        },
    )
    db.commit()
    db.refresh(analysis)
    return _serialize_analysis(analysis)


@router.get("/patients/{patient_id}/longitudinal-analyses")
def list_longitudinal_analyses(
    patient_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    if db.get(Patient, patient_id) is None:
        raise HTTPException(status_code=404, detail="Patient not found")
    analyses = list(
        db.scalars(
            select(AIAnalysis)
            .where(
                AIAnalysis.patient_id == patient_id,
                AIAnalysis.task_type == AITaskType.PATIENT_LONGITUDINAL_ANALYSIS,
            )
            .order_by(AIAnalysis.created_at.desc(), AIAnalysis.id.desc())
        ).all()
    )
    return {"items": [_serialize_analysis(analysis) for analysis in analyses]}


@router.get("/analyses/{analysis_id}")
def get_analysis(
    analysis_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> dict[str, Any]:
    analysis = db.get(AIAnalysis, analysis_id)
    if analysis is None:
        raise HTTPException(status_code=404, detail="Analysis not found")
    return _serialize_analysis(analysis)
