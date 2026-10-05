from celery import Celery  # type: ignore[import-untyped]

from app.core.config import get_settings

settings = get_settings()
celery_app = Celery("shen", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_serializer="json",
    result_serializer="json",
    result_expires=settings.celery_result_expires_seconds,
    accept_content=["json"],
    timezone=settings.app_timezone,
    enable_utc=True,
    imports=("app.workers.tasks",),
)
