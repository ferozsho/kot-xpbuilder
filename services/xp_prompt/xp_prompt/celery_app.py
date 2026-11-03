"""Celery application for dedicated KOT5 XP Prompt workers."""

from __future__ import annotations

from urllib.parse import quote_plus

from celery import Celery

from xp_prompt.config import Settings


def create_celery_app(settings: Settings | None = None) -> Celery:
    """Create an XP Prompt Celery app with an isolated Redis namespace."""
    resolved = settings or Settings.from_env()
    password = quote_plus(resolved.redis_password)
    broker = (
        f"redis://:{password}@{resolved.redis_host}:{resolved.redis_port}/"
        f"{resolved.redis_database}"
    )
    result_database = min(resolved.redis_database + 1, 15)
    backend = (
        f"redis://:{password}@{resolved.redis_host}:{resolved.redis_port}/"
        f"{result_database}"
    )

    celery = Celery(
        "xp_prompt",
        broker=broker,
        backend=backend,
        include=["xp_prompt.tasks"],
    )
    celery.conf.update(
        accept_content=["json"],
        broker_connection_retry_on_startup=True,
        enable_utc=True,
        result_accept_content=["json"],
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_serializer="json",
        result_serializer="json",
        task_soft_time_limit=300,
        task_time_limit=330,
        timezone="UTC",
        worker_prefetch_multiplier=1,
    )
    return celery


app = create_celery_app()

