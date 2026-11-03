"""Celery tasks for the XP Prompt worker.

Only one task does real work: :func:`run_job`, which claims a queued job and
hands it to the step runner. Everything else (status, steps, cancellation) lives
in the database, so a worker restart or a browser refresh can never lose the
state of a job in flight.
"""

from __future__ import annotations

import logging
from typing import Any

from xp_prompt import jobs
from xp_prompt.celery_app import app
from xp_prompt.config import Settings

logger = logging.getLogger(__name__)


@app.task(name="xp_prompt.health_probe")
def health_probe() -> dict[str, str]:
    """Return a deterministic worker liveness response."""
    return {"service": "xp-prompt-worker", "status": "alive"}


@app.task(name="xp_prompt.run_job", bind=True)
def run_job(self: Any, job_id: str) -> dict[str, Any]:
    """Claim one job and run it; a redelivery that cannot claim is a no-op."""
    settings = Settings.from_env()
    worker_id = getattr(getattr(self, "request", None), "hostname", None) or "worker"
    task_id = getattr(getattr(self, "request", None), "id", None) or ""

    # Opportunistic recovery: every executed job also sweeps leases abandoned by
    # a dead worker, so recovery does not depend on a beat process existing.
    for expired_id, status in jobs.recover_expired(settings):
        logger.info("job_recovered job=%s status=%s", expired_id, status)
        if status == jobs.QUEUED:
            run_job.delay(expired_id)

    claimed = jobs.claim_job(settings, job_id, worker_id=f"{worker_id}:{task_id}")
    if claimed is None:
        logger.info("job_claim_skipped job=%s reason=not_claimable", job_id)
        return {"job_id": job_id, "claimed": False}

    record = jobs.run_job(settings, job_id)
    logger.info("job_result job=%s status=%s", job_id, record.status)
    return {"job_id": job_id, "claimed": True, "status": record.status}


@app.task(name="xp_prompt.recover_jobs")
def recover_jobs() -> dict[str, int]:
    """Sweep expired leases and re-enqueue anything still queued.

    Safe to call on a schedule (beat entry or cron) or by hand:
    ``celery -A xp_prompt.celery_app:app call xp_prompt.recover_jobs``.
    """
    settings = Settings.from_env()
    expired = jobs.recover_expired(settings)
    requeued = [job_id for job_id, status in expired if status == jobs.QUEUED]
    pending = jobs.pending_job_ids(settings)
    for job_id in dict.fromkeys([*requeued, *pending]):
        run_job.delay(job_id)
    return {"expired": len(expired), "enqueued": len(pending), "requeued": len(requeued)}

