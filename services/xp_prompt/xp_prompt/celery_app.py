"""Celery application for dedicated KOT5 XP Prompt workers."""

from __future__ import annotations

from urllib.parse import quote_plus

from celery import Celery
from celery.schedules import crontab

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
        # Periodic maintenance runs on a beat process, so usage delivery and the
        # ledger checks happen whether or not anyone opens the dashboard (P9-F06/F10).
        # A deployment without `celery beat` simply never fires these; the API and
        # operator actions still do the same work on demand.
        beat_schedule={
            "xp-prompt-drain-usage-outbox": {
                "task": "xp_prompt.drain_usage_outbox",
                "schedule": 60.0,
                "args": (50,),
            },
            "xp-prompt-recover-jobs": {
                # A worker that dies leaves a lease behind. Sweeping every minute
                # means a crashed job resumes in about a minute instead of waiting
                # for the next job to happen to run on any worker (P11-F04).
                "task": "xp_prompt.recover_jobs",
                "schedule": 60.0,
            },
            "xp-prompt-retention-sweep": {
                # Retention is a policy, so it runs on a schedule rather than when
                # somebody remembers (P11-F06).
                "task": "xp_prompt.apply_retention",
                "schedule": crontab(hour=3, minute=30),
            },
            "xp-prompt-reconcile-usage": {
                "task": "xp_prompt.reconcile_usage",
                "schedule": crontab(minute="*/15"),
            },
            "xp-prompt-fetch-billing-exports": {
                "task": "xp_prompt.fetch_billing_exports",
                # Once a day, after most providers have closed the previous day.
                "schedule": crontab(hour=4, minute=15),
            },
        },
    )
    return celery


app = create_celery_app()

