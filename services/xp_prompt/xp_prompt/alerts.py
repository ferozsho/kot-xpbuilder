"""Budget and ledger alerts: who is told, and what has crossed a line (P10-F07).

There is no mail transport in this runtime, and inventing one here would be a
worse answer than saying so: an alert that claims to have been *sent* when it was
only written down is exactly the kind of quiet lie a settings page invites. What
this module does is decide, from the stored configuration, **whether** alerts are
enabled, **who** they are addressed to, and **which** budgets have crossed their
warning line — so the operational log and the admin view report the same thing,
and an operator can see the alert state without waiting for an inbox that does not
exist yet.

Delivery is the one piece left for P11: the recipient and the breaches are already
resolved here, so attaching a transport is a change to :func:`report` and nothing
else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from xp_prompt import budgets, settings_registry
from xp_prompt.config import Settings

#: Budget states that are worth telling someone about, most severe last.
NOTEWORTHY = ("warning", "over", "exhausted")


def recipient(settings: Settings, *, tenant_key: str = "default") -> str:
    """Where alerts are addressed. Empty means the deployment declined them."""
    values = settings_registry.live(settings, tenant_key=tenant_key)
    resolved = values.get("usage.alert_email")
    return str(resolved.value or "").strip() if resolved is not None else ""


def report(settings: Settings, *, tenant_key: str = "default") -> dict[str, Any]:
    """The alert state: enabled, addressed to whom, and what has crossed a line.

    A budget whose state is ``warning``, ``over`` or ``exhausted`` is a breach; a
    disabled budget is not, because an operator switching a limit off is a
    decision and not an incident.
    """
    address = recipient(settings, tenant_key=tenant_key)
    state = budgets.status(settings, tenant_key=tenant_key)
    breaches = [
        {
            "id": entry["id"],
            "scope": entry["scope"],
            "scope_ref": entry["scope_ref"],
            "period": entry["period"],
            "state": entry["state"],
            "percent_used": entry["percent_used"],
            "warn_percent": entry["warn_percent"],
            "cost_micros_limit": entry["cost_micros_limit"],
        }
        for entry in state["budgets"]
        if entry["state"] in NOTEWORTHY
    ]
    breaches.sort(key=lambda item: NOTEWORTHY.index(item["state"]), reverse=True)
    return {
        "tenant": tenant_key,
        "enabled": bool(address),
        "recipient": address,
        "breaches": breaches,
        "count": len(breaches),
        "generated_at": datetime.now(UTC).isoformat(),
        # Stated, not implied: the address is where an alert would go, and this
        # deployment has no transport to take it there.
        "delivered": False,
    }
