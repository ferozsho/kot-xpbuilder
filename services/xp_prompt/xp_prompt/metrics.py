"""Operational metrics, in the format an operator already scrapes (P11-F07).

The numbers here answer the questions an on-call engineer actually asks: is the
queue growing, are jobs failing, is the outbox backing up, is a provider tripping
its breaker, how much are we spending, and how close are the safeguards to their
ceilings. They are read from the durable stores on demand rather than accumulated
in process memory, because a metric that resets when a worker restarts is a metric
that hides the restart.

Exposed as Prometheus text at ``/metrics``: gauge-per-label, which is what every
scraper understands without a client library.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from xp_prompt import budgets, settings_registry, sql_gate
from xp_prompt.config import Settings
from xp_prompt.db import connect

logger = logging.getLogger(__name__)

#: Job states that are worth a gauge of their own.
JOB_TERMINAL = ("succeeded", "failed", "cancelled")


def _counts(cursor: Any, sql: str, params: tuple[Any, ...] = ()) -> dict[str, int]:
    cursor.execute(sql, params)
    return {
        str(row["label"]): int(row["value"] or 0) for row in cursor.fetchall()
    }


def jobs(settings: Settings, *, tenant_key: str = "default") -> dict[str, int]:
    """Jobs by status for one tenant, plus the stuck ones."""
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
        row = cursor.fetchone()
        if row is None:
            return {}
        tenant_id = str(row["id"])
        counts = _counts(
            cursor,
            """
            SELECT status AS label, COUNT(*) AS value
            FROM ai_job WHERE tenant_id = %s GROUP BY status
            """,
            (tenant_id,),
        )
        counts.update(
            {
                f"{label}_total": value
                for label, value in _counts(
                    cursor,
                    """
                    SELECT CASE WHEN lease_expires_at < CURRENT_TIMESTAMP
                                THEN 'lease_expired' ELSE 'leased' END AS label,
                           COUNT(*) AS value
                    FROM ai_job
                    WHERE tenant_id = %s AND status = 'running'
                    GROUP BY 1
                    """,
                    (tenant_id,),
                ).items()
            }
        )
    return counts


def outbox(settings: Settings, *, tenant_key: str = "default") -> dict[str, int]:
    """Undelivered usage events by status: the durability of the ledger, visible."""
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
        row = cursor.fetchone()
        if row is None:
            return {}
        return _counts(
            cursor,
            """
            SELECT status AS label, COUNT(*) AS value
            FROM ai_outbox_event WHERE tenant_id = %s GROUP BY status
            """,
            (str(row["id"]),),
        )


def attempts(
    settings: Settings, *, tenant_key: str = "default", hours: int = 24
) -> dict[str, Any]:
    """Provider attempts and their outcomes over a window, for the SLO cards."""
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
        row = cursor.fetchone()
        if row is None:
            return {}
        tenant_id = str(row["id"])
        by_status = _counts(
            cursor,
            """
            SELECT status AS label, COUNT(*) AS value
            FROM ai_provider_attempt
            WHERE tenant_id = %s
              AND created_at > CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')
            GROUP BY status
            """,
            (tenant_id, int(hours)),
        )
        cursor.execute(
            """
            SELECT COALESCE(AVG(duration_ms), 0) AS avg_ms,
                   COUNT(*) AS total,
                   SUM(CASE WHEN fallback THEN 1 ELSE 0 END) AS fallbacks,
                   SUM(CASE WHEN estimated_cost_micros IS NULL
                            THEN 1 ELSE 0 END) AS unpriced
            FROM ai_provider_attempt
            WHERE tenant_id = %s
              AND created_at > CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')
            """,
            (tenant_id, int(hours)),
        )
        totals = cursor.fetchone() or {}
    return {
        "by_status": by_status,
        "avg_duration_ms": float(totals.get("avg_ms") or 0),
        "total": int(totals.get("total") or 0),
        "fallbacks": int(totals.get("fallbacks") or 0),
        "unpriced": int(totals.get("unpriced") or 0),
    }


def snapshot(settings: Settings, *, tenant_key: str = "default") -> dict[str, Any]:
    """Everything the metrics endpoint reports, in one consistent read."""
    limits = settings_registry.sql_limits(settings, tenant_key=tenant_key)
    try:
        budget_state = budgets.status(settings, tenant_key=tenant_key)
    except Exception:  # noqa: BLE001 - metrics must not fail on one store
        logger.warning("metrics_budgets_unavailable", exc_info=True)
        budget_state = {"budgets": []}
    warn = sum(
        1 for entry in budget_state["budgets"] if entry["state"] in {"warning"}
    )
    over = sum(
        1
        for entry in budget_state["budgets"]
        if entry["state"] in {"over", "exhausted"}
    )
    return {
        "jobs": jobs(settings, tenant_key=tenant_key),
        "outbox": outbox(settings, tenant_key=tenant_key),
        "attempts": attempts(settings, tenant_key=tenant_key),
        "budgets": {
            "warning": warn,
            "exceeded": over,
            "total": len(budget_state["budgets"]),
        },
        "safeguards": {
            "max_rows": limits.max_rows,
            "timeout_ms": limits.timeout_ms,
            "max_concurrent": limits.max_concurrent,
            "max_bytes": limits.max_bytes,
        },
        "sql_slots_active": sql_gate.active(settings),
        "generated_at": datetime.now(UTC).isoformat(),
    }


def prometheus(settings: Settings, *, tenant_key: str = "default") -> str:
    """The same snapshot as Prometheus text, with one label for the tenant."""
    data = snapshot(settings, tenant_key=tenant_key)
    lines: list[str] = []

    def gauge(name: str, help_text: str, value: float, label: str = "") -> None:
        if not any(line.startswith(f"# HELP {name} ") for line in lines):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
        suffix = f'{{tenant="{tenant_key}",kind="{label}"}}' if label else (
            f'{{tenant="{tenant_key}"}}'
        )
        lines.append(f"{name}{suffix} {value}")

    for status, value in sorted(data["jobs"].items()):
        gauge("xp_prompt_jobs", "Jobs by status", value, status)
    for status, value in sorted(data["outbox"].items()):
        gauge(
            "xp_prompt_outbox_events", "Usage outbox events by status", value, status
        )
    for status, value in sorted(data["attempts"].get("by_status", {}).items()):
        gauge(
            "xp_prompt_provider_attempts",
            "Provider attempts by status",
            value,
            status,
        )
    gauge(
        "xp_prompt_provider_attempt_duration_ms",
        "Mean provider attempt duration",
        data["attempts"].get("avg_duration_ms", 0),
    )
    gauge(
        "xp_prompt_provider_fallbacks",
        "Attempts served by a fallback provider",
        data["attempts"].get("fallbacks", 0),
    )
    for state, value in (
        ("warning", data["budgets"]["warning"]),
        ("exceeded", data["budgets"]["exceeded"]),
    ):
        gauge("xp_prompt_budgets", "Budgets by state", value, state)
    gauge(
        "xp_prompt_sql_slots_active",
        "AI SQL execution slots in use",
        data["sql_slots_active"],
    )
    gauge(
        "xp_prompt_sql_max_rows",
        "Row cap in force",
        data["safeguards"]["max_rows"],
    )
    return "\n".join(lines) + "\n"
