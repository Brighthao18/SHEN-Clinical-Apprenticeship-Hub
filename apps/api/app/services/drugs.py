from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.domain import DrugDictionary, HospitalDrugSpecification

SEED_DRUGS: dict[str, tuple[str, str | None]] = {
    "鳖甲": ("鳖甲", None),
    "醋鳖甲": ("鳖甲", "醋制"),
    "白花蛇舌草": ("白花蛇舌草", None),
    "半枝莲": ("半枝莲", None),
    "茯神": ("茯神", None),
    "知母": ("知母", None),
    "枳壳": ("枳壳", None),
    "柿蒂": ("柿蒂", None),
    "益智仁": ("益智仁", None),
    "桑螵蛸": ("桑螵蛸", None),
    "红景天": ("红景天", None),
    "藏红曲": ("藏红曲", None),
}

PROCESSING_NORMALIZATION = {
    "醋": "醋制",
    "醋制": "醋制",
    "麸炒": "麸炒",
    "炒": "炒制",
    "酒": "酒制",
    "盐": "盐制",
}

# Same vocabulary the ingestion pipeline normalizes to: 包/袋 both mean one
# decoction package, so the stored reference unit is always ``package``.
PACKAGE_UNIT_NORMALIZATION = {"包": "package", "袋": "package"}


class SpecificationExistsError(Exception):
    """An identical manual specification version is already stored."""


def create_manual_verified_specification(
    session: Session,
    *,
    hospital: str,
    dictionary_id,
    package_dose_g: Decimal,
    original_package_unit: str,
    processing_method: str | None,
    effective_from: date | None,
    effective_to: date | None,
    source_note: str | None,
) -> HospitalDrugSpecification:
    """Store a user-entered ``manual_verified`` hospital specification.

    This is the only sanctioned write path for reference specifications: the
    dose comes from the user's own source (e.g. the pharmacy label), never
    from AI or heuristics. The package unit is normalized exactly like the
    ingestion pipeline (包/袋 -> ``package``) and the processing method is
    normalized with the same map used at extraction so later
    ``find_effective_specification`` lookups match.
    """
    normalized_unit = PACKAGE_UNIT_NORMALIZATION.get(original_package_unit.strip())
    if normalized_unit is None:
        raise ValueError(f"Unsupported package unit: {original_package_unit}")

    normalized_processing = (processing_method or "").strip() or None
    if normalized_processing:
        normalized_processing = PROCESSING_NORMALIZATION.get(
            normalized_processing, normalized_processing
        )

    version = get_settings().drug_dictionary_version
    duplicate = session.scalar(
        select(HospitalDrugSpecification).where(
            HospitalDrugSpecification.hospital == hospital.strip(),
            HospitalDrugSpecification.drug_dictionary_id == dictionary_id,
            HospitalDrugSpecification.normalized_package_unit == normalized_unit,
            HospitalDrugSpecification.processing_method.is_(None)
            if normalized_processing is None
            else HospitalDrugSpecification.processing_method == normalized_processing,
            HospitalDrugSpecification.effective_from.is_(None)
            if effective_from is None
            else HospitalDrugSpecification.effective_from == effective_from,
            HospitalDrugSpecification.version == version,
        )
    )
    if duplicate is not None:
        raise SpecificationExistsError(
            "An identical specification version already exists for this drug"
        )

    specification = HospitalDrugSpecification(
        hospital=hospital.strip(),
        drug_dictionary_id=dictionary_id,
        processing_method=normalized_processing,
        package_dose_g=package_dose_g,
        normalized_package_unit=normalized_unit,
        effective_from=effective_from,
        effective_to=effective_to,
        source_type="manual_verified",
        source_note=(source_note or "").strip() or None,
        version=version,
    )
    session.add(specification)
    session.flush()
    return specification


@dataclass(frozen=True)
class DrugNormalizationResult:
    canonical_name: str | None
    processing_method: str | None
    confidence: Decimal
    dictionary: DrugDictionary | None


def normalize_drug(
    session: Session, raw_name: str, extracted_processing: str | None
) -> DrugNormalizationResult:
    seed = SEED_DRUGS.get(raw_name.strip())
    if seed is None:
        return DrugNormalizationResult(None, extracted_processing, Decimal("0.5000"), None)

    canonical, seed_processing = seed
    processing = extracted_processing or seed_processing
    if processing:
        processing = PROCESSING_NORMALIZATION.get(processing, processing)

    version = get_settings().drug_dictionary_version
    query = select(DrugDictionary).where(
        DrugDictionary.canonical_name == canonical,
        DrugDictionary.dictionary_version == version,
        # Phase 6.2: only VERIFIED entries resolve; PENDING candidates and
        # DISABLED (known-bad) entries never produce canonical mappings.
        DrugDictionary.status == "VERIFIED",
    )
    if processing is None:
        query = query.where(DrugDictionary.processing_method.is_(None))
    else:
        query = query.where(DrugDictionary.processing_method == processing)
    dictionary = session.scalar(query)
    if dictionary is None:
        dictionary = DrugDictionary(
            canonical_name=canonical,
            processing_method=processing,
            aliases=[raw_name] if raw_name != canonical else [],
            hospital_display_names=[raw_name],
            common_ocr_errors=[],
            dictionary_version=version,
            effective_from=date(2026, 8, 28),
            source="project seed dictionary; requires clinical curation",
            status="VERIFIED",
        )
        session.add(dictionary)
        session.flush()
    return DrugNormalizationResult(canonical, processing, Decimal("0.9900"), dictionary)


def raw_identity_verifiable(raw_name: str, original_text: str | None) -> bool:
    """Deterministic check that a raw drug name is itself trustworthy.

    Phase 6.1: a merely missing canonical mapping must not block ingestion,
    but raw identity must be proven before an item is stored. The bounded
    proxy for "identity unproven / garbled / non-drug":

    - the name consists solely of CJK characters (no digits, latin, or
      punctuation debris);
    - the name occurs inside the item's own cited OCR evidence span.

    If either fails, the item cannot be attributed to any real herb in the
    source and ingestion stays blocked.
    """

    if not raw_name or not original_text:
        return False
    name = raw_name.strip()
    if not name or len(name) > 20:
        return False
    if not all("\u4e00" <= character <= "\u9fff" for character in name):
        return False
    return name in "".join(str(original_text).split())


def resolve_drug_with_suggestion(
    session: Session, raw_name: str, processing: str | None, suggested_canonical: str | None
) -> DrugNormalizationResult:
    """Resolve identity through the dictionary, optionally via an AI proposal.

    Resolution order (Phase 6.2):
    1. a VERIFIED entry whose candidate_raw_name matches the printed raw
       name — the "verify once, reuse forever" path;
    2. the seed raw-name map (alias path);
    3. an AI-suggested canonical name narrowed against existing VERIFIED
       entries.

    Suggestions never create canonical drugs; an unmatched suggestion stays
    pending and the item is stored marked as such.
    """

    verified_candidate = session.scalar(
        select(DrugDictionary).where(
            DrugDictionary.candidate_raw_name == raw_name.strip(),
            DrugDictionary.status == "VERIFIED",
        )
    )
    if verified_candidate is not None:
        processing = verified_candidate.processing_method or (
            PROCESSING_NORMALIZATION.get(processing, processing) if processing else None
        )
        return DrugNormalizationResult(
            verified_candidate.canonical_name, processing, Decimal("0.9500"), verified_candidate
        )

    result = normalize_drug(session, raw_name, processing)
    if result.dictionary is not None or not suggested_canonical:
        return result
    suggested = suggested_canonical.strip()
    normalized_processing = processing or None
    if normalized_processing:
        normalized_processing = PROCESSING_NORMALIZATION.get(
            normalized_processing, normalized_processing
        )
    version = get_settings().drug_dictionary_version
    query = select(DrugDictionary).where(
        DrugDictionary.canonical_name == suggested,
        DrugDictionary.dictionary_version == version,
        DrugDictionary.status == "VERIFIED",
    )
    if normalized_processing is None:
        query = query.where(DrugDictionary.processing_method.is_(None))
    else:
        query = query.where(DrugDictionary.processing_method == normalized_processing)
    dictionary = session.scalar(query)
    if dictionary is None:
        return result
    return DrugNormalizationResult(suggested, normalized_processing, Decimal("0.9000"), dictionary)


def find_effective_specification(
    session: Session,
    *,
    hospital: str,
    dictionary_id,
    processing_method: str | None,
    normalized_package_unit: str,
    encounter_date: date,
) -> HospitalDrugSpecification | None:
    statement = (
        select(HospitalDrugSpecification)
        .where(
            HospitalDrugSpecification.hospital == hospital,
            HospitalDrugSpecification.drug_dictionary_id == dictionary_id,
            HospitalDrugSpecification.normalized_package_unit == normalized_package_unit,
            HospitalDrugSpecification.source_type == "manual_verified",
            or_(
                HospitalDrugSpecification.effective_from.is_(None),
                HospitalDrugSpecification.effective_from <= encounter_date,
            ),
            or_(
                HospitalDrugSpecification.effective_to.is_(None),
                HospitalDrugSpecification.effective_to >= encounter_date,
            ),
        )
        .order_by(
            HospitalDrugSpecification.effective_from.is_(None),
            HospitalDrugSpecification.effective_from.desc(),
            HospitalDrugSpecification.version.desc(),
        )
    )
    if processing_method is None:
        statement = statement.where(HospitalDrugSpecification.processing_method.is_(None))
    else:
        statement = statement.where(
            HospitalDrugSpecification.processing_method == processing_method
        )
    return session.scalars(statement).first()


def calculate_dose(
    package_dose_g: Decimal | None,
    package_count: Decimal | None,
    specification: HospitalDrugSpecification | None = None,
) -> Decimal | None:
    if package_count is None:
        return None
    if package_dose_g is not None:
        return package_dose_g * package_count
    if specification is not None:
        return specification.package_dose_g * package_count
    return None


def calculate_course_total_dose(
    per_decoction_dose_g: Decimal | None,
    total_decoctions: int | None,
) -> Decimal | None:
    if per_decoction_dose_g is None or total_decoctions is None or total_decoctions <= 0:
        return None
    return per_decoction_dose_g * Decimal(total_decoctions)
