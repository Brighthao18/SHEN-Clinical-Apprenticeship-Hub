from fastapi import APIRouter

from app.api.routes import (
    analyses,
    auth,
    backup_status,
    dashboard,
    documents,
    drug_dictionary,
    imports,
    ingestion,
    notes,
    patients,
    prescription_diffs,
    reviews,
    settings,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(backup_status.router)
api_router.include_router(dashboard.router)
api_router.include_router(imports.router)
api_router.include_router(documents.router)
api_router.include_router(patients.router)
api_router.include_router(prescription_diffs.router)
api_router.include_router(reviews.router)
api_router.include_router(ingestion.router)
api_router.include_router(analyses.router)
api_router.include_router(notes.router)
api_router.include_router(settings.router)
api_router.include_router(drug_dictionary.router)
