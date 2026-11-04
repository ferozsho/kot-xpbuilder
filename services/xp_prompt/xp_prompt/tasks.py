"""Celery tasks for the XP Prompt worker.

Only one task does real work: :func:`run_job`, which claims a queued job and
hands it to the step runner. Everything else (status, steps, cancellation) lives
in the database, so a worker restart or a browser refresh can never lose the
state of a job in flight.
"""

from __future__ import annotations

import logging
from typing import Any

from xp_prompt import alerts, billing, jobs, usage
from xp_prompt.celery_app import app
from xp_prompt.config import Settings
from xp_prompt.providers.registry import DEFAULT_TENANT_KEY

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


@app.task(name="xp_prompt.drain_usage_outbox")
def drain_usage_outbox(limit: int = 25) -> dict[str, Any]:
    """Deliver usage events the request path could not apply (P9-F06).

    Recording is attempted inline so the admin totals are current, but anything
    that failed then is still in the outbox. This task is the retry that makes
    "no lost records" true rather than hoped for.

    Safe to run on a schedule or by hand:
    ``celery -A xp_prompt.celery_app:app call xp_prompt.drain_usage_outbox``.
    """
    settings = Settings.from_env()
    outcome = usage.drain(settings, limit=limit)
    logger.info(
        "usage_outbox_drained claimed=%d delivered=%d duplicates=%d failed=%d",
        outcome["claimed"],
        outcome["delivered"],
        outcome["duplicates"],
        outcome["failed"],
    )
    return outcome


@app.task(name="xp_prompt.reconcile_usage")
def reconcile_usage() -> dict[str, Any]:
    """Check every tenant's ledger on a schedule, not when someone remembers.

    Each finding is counted and logged at a severity an operator will notice; the
    point is that a negative token count or a dead-lettered event is discovered by
    the system rather than by a support ticket. A tenant that cannot be read is
    reported and skipped so one broken row cannot stop the sweep.
    """
    settings = Settings.from_env()
    reports: dict[str, Any] = {}
    unhealthy: list[str] = []
    for tenant_key in usage.tenant_keys(settings):
        try:
            report = usage.reconcile(settings, tenant_key=tenant_key)
        except Exception:  # pragma: no cover - one tenant must not stop the sweep
            logger.warning("usage_reconcile_failed tenant=%s", tenant_key,
                           exc_info=True)
            continue
        reports[tenant_key] = report
        if not report["healthy"]:
            unhealthy.append(tenant_key)
            logger.warning(
                "usage_reconcile_unhealthy tenant=%s negative=%d unfinished=%d "
                "inconsistent=%d dead_letter=%d pending=%d",
                tenant_key,
                report["negative_tokens"],
                report["unfinished"],
                report["inconsistent_totals"],
                report["outbox_dead_letter"],
                report["outbox_pending"],
            )
        elif report["unpriced"] or report["missing_usage"]:
            logger.info(
                "usage_reconcile_gaps tenant=%s unpriced=%d missing_usage=%d "
                "price_gaps=%s",
                tenant_key,
                report["unpriced"],
                report["missing_usage"],
                ",".join(report["price_gaps"]) or "-",
            )
        _report_breaches(settings, tenant_key)
    return {"tenants": len(reports), "unhealthy": unhealthy, "reports": reports}


def _report_breaches(settings: Settings, tenant_key: str) -> None:
    """Name the budgets that crossed their warning line, and who is addressed.

    The alert is emitted here rather than in a route because a budget breach is
    discovered when the window moves, not when an admin opens a page. No mail is
    sent — there is no transport in this runtime — so the log line records the
    recipient the stored setting names and nothing claims delivery (P10-F07).
    """
    try:
        state = alerts.report(settings, tenant_key=tenant_key)
    except Exception:  # pragma: no cover - a sweep must not fail on an alert
        logger.warning("usage_alerts_failed tenant=%s", tenant_key, exc_info=True)
        return
    if not state["breaches"]:
        return
    worst = state["breaches"][0]
    logger.warning(
        "usage_alert tenant=%s breaches=%d worst=%s percent=%d recipient=%s "
        "delivered=false",
        tenant_key,
        state["count"],
        worst["state"],
        worst["percent_used"],
        state["recipient"] or "-",
    )


@app.task(name="xp_prompt.fetch_billing_exports")
def fetch_billing_exports() -> dict[str, Any]:
    """Pull the provider's billing export when a source is configured (P9-F10).

    Unconfigured is not a failure: a deployment may prefer to paste exports by
    hand, and pretending to fetch would be worse than saying so.
    """
    settings = Settings.from_env()
    status = billing.source_status(settings)
    if not status["configured"]:
        logger.info("billing_fetch_skipped reason=not_configured")
        return {"fetched": False, "reason": "not_configured"}
    try:
        outcome = billing.fetch_export(settings, tenant_key=DEFAULT_TENANT_KEY)
    except billing.BillingError as exc:
        logger.warning("billing_fetch_failed code=%s message=%s", exc.code, exc)
        return {"fetched": False, "reason": exc.code, "message": str(exc)}
    return {"fetched": True, **outcome}

