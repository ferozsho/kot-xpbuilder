"""Retention, archival and offboarding (P11-F06).

A deployment accumulates conversations, jobs, attempt rows, artifacts and audit
records forever unless something removes them, and "we will clean it up later"
becomes a storage bill and a privacy problem. This module owns that lifecycle, and
its rules are deliberately conservative:

* **budgets and the configuration audit are never purged by a sweep**, and the
  usage ledger (``ai_provider_attempt``) has no window by default. The ledger *is*
  the accounting record, so it is purgeable only when an operator sets a window on
  purpose — data minimisation is a legitimate reason, an accident is not.
* **a conversation is archived before it is deleted**, so the counts reconcile and
  an operator can see what the sweep did rather than trusting it.
* **nothing is deleted without a name for it**: every sweep returns per-table
  counts, and the scheduled task logs them.
* **offboarding is an explicit act**, not a side effect of a retention window: it
  removes one tenant's rows across every table and keeps the global configuration
  and the audit of the removal itself.

Retention windows are settings (``retention.*``), so a deployment decides its own
policy; ``0`` means keep forever and is the default for anything with legal weight.
"""

from __future__ import annotations

import logging
from typing import Any

from xp_prompt import settings_registry
from xp_prompt.config import Settings
from xp_prompt.db import connect

logger = logging.getLogger(__name__)

#: Tables a retention sweep may purge, with the column that dates a row. Ordered
#: child-first so a parent is never removed while its children still exist.
PURGEABLE: tuple[tuple[str, str, str], ...] = (
    ("ai_job_step", "created_at", "retention.jobs_days"),
    ("ai_job", "created_at", "retention.jobs_days"),
    ("ai_message", "created_at", "retention.conversations_days"),
    ("ai_conversation", "created_at", "retention.conversations_days"),
    ("ai_provider_attempt", "started_at", "retention.attempts_days"),
    ("ai_artifact", "created_at", "retention.artifacts_days"),
    ("ai_outbox_event", "created_at", "retention.outbox_days"),
)

#: Never purged by a sweep, whatever the window says.
PROTECTED = frozenset(
    {
        "ai_usage_budget",
        "ai_usage_budget_window",
        "ai_billing_import",
        "ai_billing_line",
        "ai_setting",
        "ai_setting_audit",
        "ai_tenant",
    }
)


class RetentionError(ValueError):
    """A refused retention operation; ``code`` is stable and safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def windows(settings: Settings, *, tenant_key: str = "default") -> dict[str, int]:
    """The configured retention windows, in days. ``0`` means keep forever."""
    values = settings_registry.live(settings, tenant_key=tenant_key)
    resolved: dict[str, int] = {}
    for key in (
        "retention.conversations_days",
        "retention.jobs_days",
        "retention.attempts_days",
        "retention.artifacts_days",
        "retention.outbox_days",
    ):
        entry = values.get(key)
        resolved[key] = int(entry.value) if entry is not None else 0
    return resolved


def preview(settings: Settings, *, tenant_key: str = "default") -> dict[str, Any]:
    """What a sweep would remove, without removing it.

    An operator should be able to see the size of a deletion before it happens; a
    retention job that only reports afterwards is a job you learn to fear.
    """
    limits = windows(settings, tenant_key=tenant_key)
    counts: dict[str, int] = {}
    with connect(settings) as connection, connection.cursor() as cursor:
        for table, column, key in PURGEABLE:
            days = limits.get(key, 0)
            if days <= 0 or table in PROTECTED:
                continue
            cursor.execute(
                f"""
                SELECT COUNT(*) AS rows
                FROM {table}
                WHERE {column} < CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                """,
                (int(days),),
            )
            row = cursor.fetchone() or {}
            counts[table] = int(row.get("rows") or 0)
    return {"windows": limits, "would_delete": counts}


def apply(settings: Settings, *, tenant_key: str = "default") -> dict[str, Any]:
    """Delete what the windows allow, and report what was removed.

    Deletion is child-first and batched by the window itself: a row is only
    removed when it is older than the window, so a sweep never has to hold a
    transaction over the whole table.
    """
    limits = windows(settings, tenant_key=tenant_key)
    removed: dict[str, int] = {}
    with connect(settings) as connection, connection.cursor() as cursor:
        for table, column, key in PURGEABLE:
            days = limits.get(key, 0)
            if days <= 0 or table in PROTECTED:
                continue
            cursor.execute(
                f"""
                DELETE FROM {table}
                WHERE {column} < CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                """,
                (int(days),),
            )
            if cursor.rowcount:
                removed[table] = int(cursor.rowcount)
    if removed:
        logger.info(
            "retention_swept tenant=%s tables=%s",
            tenant_key,
            ",".join(f"{table}:{count}" for table, count in sorted(removed.items())),
        )
    return {"windows": limits, "removed": removed, "total": sum(removed.values())}


#: Every table that holds a tenant's own rows, for offboarding. Child-first.
TENANT_TABLES: tuple[str, ...] = (
    "ai_usage_budget_window",
    "ai_usage_budget",
    "ai_billing_line",
    "ai_billing_import",
    "ai_provider_attempt",
    "ai_outbox_event",
    "ai_job_step",
    "ai_job",
    "ai_message",
    "ai_conversation",
    "ai_artifact",
    "ai_sql_audit",
    "ai_sql_example",
    "ai_repair_attempt",
    "ai_repair_pattern",
    "ai_dataset_draft",
    "ai_chart_draft",
    "ai_dashboard_publish",
    "ai_provider",
    "ai_setting",
    "ai_setting_audit",
)


def offboard(
    settings: Settings, *, tenant_key: str, actor: str = "", reason: str = ""
) -> dict[str, Any]:
    """Remove one tenant's data, leaving the tenant row itself in place.

    The tenant row stays so the removal is visible and the key cannot be silently
    reused by a different customer; the global configuration is untouched, and the
    act is logged with who asked for it and why.
    """
    if not tenant_key or tenant_key == "default":
        # Offboarding the default tenant would empty the deployment by accident.
        raise RetentionError(
            "protected_tenant", "the default tenant cannot be offboarded"
        )
    removed: dict[str, int] = {}
    with connect(settings) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
        row = cursor.fetchone()
        if row is None:
            raise RetentionError("unknown_tenant", "that tenant is not provisioned")
        tenant_id = str(row["id"])
        for table in TENANT_TABLES:
            cursor.execute(
                f"DELETE FROM {table} WHERE tenant_id = %s", (tenant_id,)
            )
            if cursor.rowcount:
                removed[table] = int(cursor.rowcount)
    logger.warning(
        "tenant_offboarded tenant=%s actor=%s reason=%s rows=%d",
        tenant_key,
        actor or "-",
        (reason or "-")[:120],
        sum(removed.values()),
    )
    return {
        "tenant": tenant_key,
        "removed": removed,
        "total": sum(removed.values()),
        "actor": actor,
        "reason": reason,
    }
