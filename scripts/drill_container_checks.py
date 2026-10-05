"""Container-side checks for the Phase 5.2 production restore drill.

Executed inside the production api container via
``docker compose exec -T api python - < scripts/drill_container_checks.py``
with ``DRILL_DATABASE`` naming the isolated restored database. Secrets stay
inside the container; output is booleans and counts only.
"""

from __future__ import annotations

import json
import os
import sys
import uuid


def isolated_pii_roundtrip() -> dict[str, object]:
    """Prove the restored schema accepts and exactly retrieves PII.

    Uses only synthetic fake values inside the throwaway isolated database.
    """

    original_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = original_url.rsplit("/", 1)[0] + "/" + os.environ["DRILL_DATABASE"]
    for module in list(sys.modules):
        if module.startswith("app."):
            del sys.modules[module]

    from app.db.session import SessionLocal
    from app.db.types import pii_fingerprint
    from app.models.domain import Patient, PatientIdentity
    from sqlalchemy import select, text

    fake_name = "恢复演练合成姓名"
    fake_mrn = f"DRILL-{uuid.uuid4().hex[:8]}"
    with SessionLocal() as session:
        patient = Patient(internal_patient_code=f"DRILL{uuid.uuid4().hex[:6]}", sex="female")
        session.add(patient)
        session.flush()
        identity = PatientIdentity(
            patient_id=patient.id,
            hospital="drill-hospital",
            real_name=fake_name,
            hospital_medical_record_number=fake_mrn,
            real_name_fingerprint=pii_fingerprint(fake_name),
            mrn_fingerprint=pii_fingerprint(fake_mrn),
        )
        session.add(identity)
        session.commit()

        found = session.scalar(
            select(PatientIdentity).where(
                PatientIdentity.hospital == "drill-hospital",
                PatientIdentity.mrn_fingerprint == pii_fingerprint(fake_mrn),
            )
        )
        raw_column = session.execute(
            text("SELECT real_name FROM patient_identities WHERE id = :id"),
            {"id": identity.id},
        ).scalar_one()
        return {
            "hmac_exact_lookup_found": found is not None,
            "ciphertext_decrypts_to_original": bool(
                found is not None and found.real_name == fake_name
            ),
            "stored_value_is_not_plaintext": fake_name not in str(raw_column),
        }


def app_smoke() -> dict[str, object]:
    """Authenticated + anonymous API checks against the running service."""

    import httpx
    from app.core.security import create_access_token

    token, _ = create_access_token(os.environ.get("APP_ADMIN_USERNAME", "admin"))
    headers = {"Authorization": f"Bearer {token}"}
    results: dict[str, object] = {}
    with httpx.Client(base_url="http://localhost:8000/api/v1", timeout=15) as client:
        results["health_ok"] = client.get("http://localhost:8000/health").status_code == 200
        results["patients_authorized"] = client.get("/patients", headers=headers).status_code == 200
        results["dashboard_authorized"] = client.get("/dashboard", headers=headers).status_code == 200
        results["unresolved_queue_authorized"] = (
            client.get("/ingestion/unresolved", headers=headers).status_code == 200
        )
        results["documents_anonymous_rejected"] = (
            client.get(f"/documents/{uuid.uuid4()}/content").status_code == 401
        )
        results["login_rejects_wrong_password"] = (
            client.post(
                "/auth/token",
                data={"username": os.environ.get("APP_ADMIN_USERNAME", "admin"), "password": "definitely-wrong"},
            ).status_code
            == 401
        )
    return results


if __name__ == "__main__":
    payload: dict[str, object] = {"status": "drill_container_checks_complete"}
    if os.environ.get("DRILL_DATABASE"):
        payload["isolated_pii_roundtrip"] = isolated_pii_roundtrip()
    payload["app_smoke"] = app_smoke()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
