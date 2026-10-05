FROM python:3.12-slim@sha256:02108f5d322dd89f1c9e552442c25acb0543dfdbc455693a5599624f20d9155d

LABEL org.opencontainers.image.licenses="Apache-2.0" org.opencontainers.image.version="0.2.0-rc.1"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml requirements.lock LICENSE NOTICE ./
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN python -m pip install --require-hashes -r requirements.lock && python -m pip install --no-deps --no-build-isolation .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data/storage \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
