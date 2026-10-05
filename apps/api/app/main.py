from __future__ import annotations

import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import engine

configure_logging()
settings = get_settings()

app = FastAPI(
    title="师承临床知识管理 API",
    version=settings.app_version,
    description="Local-first, provenance-aware clinical knowledge API",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.web_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/health", tags=["health"])
def health() -> JSONResponse:
    """Readiness probe: 200 only when the process AND its database work.

    The container healthcheck in compose.yaml hits this endpoint, so a
    database outage must fail it (503) instead of hiding behind a 200 body
    flag. Liveness-only callers should use ``/health/live``.
    """

    database = "ok"
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        database = "unavailable"
    payload = {
        "service": "shen-clinical-api",
        "version": settings.app_version,
        "database": database,
        "ocr_provider": settings.ocr_provider,
    }
    return JSONResponse(status_code=200 if database == "ok" else 503, content=payload)


@app.get("/health/live", tags=["health"])
def health_live() -> dict[str, str]:
    """Liveness probe: the API process is up, regardless of dependencies."""

    return {"service": "shen-clinical-api", "status": "alive"}


app.include_router(api_router)
