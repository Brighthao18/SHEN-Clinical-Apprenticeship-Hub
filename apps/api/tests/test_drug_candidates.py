"""Phase 6.2 drug dictionary candidate lifecycle tests."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.domain import DrugDictionary, PrescriptionItem
from app.services.ai.extractor import evidence_key

FIXTURE_OCR = (Path(__file__).parent / "fixtures" / "jnu_outpatient_v1.txt").read_text(
    encoding="utf-8"
)
DEFAULT_REGISTRY = Path(__file__).parent / "fixtures" / "ai_extraction_fixtures.json"


def _base_payload() -> dict:
    registry = json.loads(DEFAULT_REGISTRY.read_text(encoding="utf-8"))
    key = hashlib.sha256(evidence_key(FIXTURE_OCR).encode("utf-8")).hexdigest()
    return deepcopy(registry["entries"][key])


_upload_counter = {"n": 0}


def _upload_with_herb(client, auth_headers, monkeypatch, tmp_path: Path, herb_line: str) -> dict:
    """Upload the fixture case plus one extra herb line (raw name = AI suggestion).

    Bytes are unique per call so the sha256 duplicate guard does not merge the
    two imports — these tests exercise the multi-encounter reuse path.
    """

    herb = herb_line.split("（")[0]
    payload = _base_payload()
    payload["prescription"]["raw_text"] = "共7剂\n每日1剂"
    payload["prescription"]["items"].append(
        {
            "original_text": herb_line,
            "raw_drug_name": herb,
            "processing_method": None,
            "suggested_canonical_name": herb,
            "is_basic_drug": False,
            "explicit_package_dose_g": 10,
            "package_count": 1,
            "package_unit": "包",
            "confidence": 0.97,
        }
    )
    payload["patient"]["medical_record_number"] = f"JNU-CAND-{_upload_counter['n']}"
    ocr_text = FIXTURE_OCR + "\n" + herb_line
    key = hashlib.sha256(evidence_key(ocr_text).encode("utf-8")).hexdigest()
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps({"version": 1, "entries": {key: payload}}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        get_settings(), "ai_extraction_fixture_path", registry, raising=False
    )
    _upload_counter["n"] += 1
    return client.post(
        "/api/v1/imports",
        headers=auth_headers,
        files={
            "files": (
                f"visit-{_upload_counter['n']}.jpg",
                f"candidate-test-{_upload_counter['n']}".encode(),
                "image/jpeg",
            )
        },
        data={"mock_ocr_text": ocr_text, "label": "candidate"},
    )


def _pending_entries(client, auth_headers) -> list[dict]:
    return client.get("/api/v1/drug-dictionary?status=PENDING", headers=auth_headers).json()


def test_candidate_created_once_and_backfilled_on_verify(
    client, auth_headers, monkeypatch, tmp_path
) -> None:
    first = _upload_with_herb(client, auth_headers, monkeypatch, tmp_path, "威灵仙（10g）1包")
    second = _upload_with_herb(client, auth_headers, monkeypatch, tmp_path, "威灵仙（10g）1包")
    assert first.json()["status"] == "ACCEPTED"
    assert second.json()["status"] == "ACCEPTED"

    with SessionLocal() as session:
        candidates = session.scalars(
            select(DrugDictionary).where(DrugDictionary.status == "PENDING")
        ).all()
        # Same raw drug reuses exactly ONE candidate; usage accumulates.
        assert len(candidates) == 1
        assert candidates[0].usage_count == 2
        assert candidates[0].candidate_raw_name == "威灵仙"
        items = session.scalars(
            select(PrescriptionItem).where(PrescriptionItem.raw_drug_name == "威灵仙")
        ).all()
        assert len(items) == 2
        assert all(item.canonical_drug_name is None for item in items)
        assert all(item.normalization_status == "pending" for item in items)
        assert all(item.ai_suggested_canonical_name == "威灵仙" for item in items)

    listing = _pending_entries(client, auth_headers)
    assert len(listing) == 1
    assert listing[0]["usage_count"] == 2
    evidence = listing[0]["representative_evidence"] or ""
    assert "威灵仙" in evidence

    verified = client.post(
        f"/api/v1/drug-dictionary/{listing[0]['id']}/verify",
        headers=auth_headers,
        json={"canonical_name": "威灵仙", "processing_method": None},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["backfilled_items"] == 2

    with SessionLocal() as session:
        items = session.scalars(
            select(PrescriptionItem).where(PrescriptionItem.raw_drug_name == "威灵仙")
        ).all()
        assert all(item.normalization_status == "normalized" for item in items)
        assert all(item.canonical_drug_name == "威灵仙" for item in items)
        assert all(item.drug_dictionary_id is not None for item in items)
        # Raw name is preserved even after canonical mapping.
        assert all(item.raw_drug_name == "威灵仙" for item in items)

    # Verify once, reuse forever: the next occurrence resolves directly.
    third = _upload_with_herb(client, auth_headers, monkeypatch, tmp_path, "威灵仙（10g）1包")
    assert third.json()["status"] == "ACCEPTED"
    with SessionLocal() as session:
        item = session.scalar(
            select(PrescriptionItem)
            .where(PrescriptionItem.raw_drug_name == "威灵仙")
            .order_by(PrescriptionItem.created_at.desc())
        )
        assert item is not None
        assert item.normalization_status == "normalized"
        assert item.canonical_drug_name == "威灵仙"
        assert not _pending_entries(client, auth_headers)


def test_disabled_entry_never_resolves(client, auth_headers, monkeypatch, tmp_path) -> None:
    first = _upload_with_herb(client, auth_headers, monkeypatch, tmp_path, "威灵仙（10g）1包")
    assert first.json()["status"] == "ACCEPTED"
    listing = _pending_entries(client, auth_headers)
    disabled = client.post(
        f"/api/v1/drug-dictionary/{listing[0]['id']}/disable", headers=auth_headers
    )
    assert disabled.status_code == 200

    again = _upload_with_herb(client, auth_headers, monkeypatch, tmp_path, "威灵仙（10g）1包")
    assert again.json()["status"] == "ACCEPTED"
    with SessionLocal() as session:
        item = session.scalar(
            select(PrescriptionItem)
            .where(PrescriptionItem.raw_drug_name == "威灵仙")
            .order_by(PrescriptionItem.created_at.desc())
        )
        # A disabled (known-bad) mapping never resolves; the item stays pending.
        assert item is not None
        assert item.canonical_drug_name is None
        assert item.normalization_status == "pending"


def test_batch_verify_accepts_suggestions(client, auth_headers, monkeypatch, tmp_path) -> None:
    _upload_with_herb(client, auth_headers, monkeypatch, tmp_path, "威灵仙（10g）1包")
    _upload_with_herb(client, auth_headers, monkeypatch, tmp_path, "威灵仙（10g）1包")
    pending = _pending_entries(client, auth_headers)
    assert len(pending) == 1 and pending[0]["usage_count"] == 2

    batch = client.post(
        "/api/v1/drug-dictionary/batch-verify",
        headers=auth_headers,
        json={"ids": [pending[0]["id"]]},
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["backfilled_items"] == 2
    assert _pending_entries(client, auth_headers) == []
