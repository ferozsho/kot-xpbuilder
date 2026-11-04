"""Budget and ledger alerts: who is told, and what has crossed a line (P10-F07).

An alert that claims to have been *sent* when it was only written down is the
kind of quiet lie a settings page invites, so this module separates the two:
:func:`report` says whether alerts are enabled, who they are addressed to, and
which budgets crossed their warning line; :func:`deliver` actually posts that
report to the configured webhook and reports what happened. Without a webhook the
report is still resolved and the reason it was not sent is stated.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import UTC, datetime
from typing import Any

from xp_prompt import billing, budgets, settings_registry
from xp_prompt.config import Settings

logger = logging.getLogger(__name__)

#: Budget states that are worth telling someone about, most severe last.
NOTEWORTHY = ("warning", "over", "exhausted")


def recipient(settings: Settings, *, tenant_key: str = "default") -> str:
    """Where alerts are addressed. Empty means the deployment declined them."""
    values = settings_registry.live(settings, tenant_key=tenant_key)
    resolved = values.get("usage.alert_email")
    return str(resolved.value or "").strip() if resolved is not None else ""


def webhook_url(settings: Settings, *, tenant_key: str = "default") -> str:
    """The https endpoint a breach report is posted to, or empty."""
    values = settings_registry.live(settings, tenant_key=tenant_key)
    resolved = values.get("usage.alert_webhook_url")
    return str(resolved.value or "").strip() if resolved is not None else ""


def deliver(
    settings: Settings, *, tenant_key: str = "default", payload: Any = None
) -> dict[str, Any]:
    """Post a breach report to the configured webhook, and say what happened.

    ``billing._assert_fetchable`` is reused: an admin-supplied URL that the server
    dereferences is the same SSRF primitive a billing source is, so it gets the
    same refusals rather than a second, weaker implementation.
    """
    state = (
        payload
        if isinstance(payload, dict)
        else report(settings, tenant_key=tenant_key)
    )
    url = webhook_url(settings, tenant_key=tenant_key)
    if not state["breaches"]:
        return {"delivered": False, "reason": "nothing_to_send", "state": state}
    if not url:
        return {"delivered": False, "reason": "not_configured", "state": state}
    try:
        billing._assert_fetchable(url)  # noqa: SLF001 - one SSRF guard, one place
    except Exception as exc:  # noqa: BLE001 - a refused URL is reported, not raised
        return {
            "delivered": False,
            "reason": getattr(exc, "code", "invalid_url"),
            "state": state,
        }
    body = json.dumps({**state, "kind": "usage_alert"}).encode("utf-8")
    request_obj = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request_obj, timeout=10) as response:
            accepted = 200 <= int(response.status) < 300
    except Exception as exc:  # noqa: BLE001 - delivery is best-effort
        logger.warning("usage_alert_delivery_failed reason=%s", type(exc).__name__)
        return {"delivered": False, "reason": "transport_error", "state": state}
    state["delivered"] = accepted
    return {
        "delivered": accepted,
        "reason": "" if accepted else "rejected",
        "state": state,
    }


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
        # Stated, not implied: ``recipient`` is who the report names, and
        # ``delivered`` only becomes true when a webhook accepted it.
        "delivered": False,
        "webhook": bool(webhook_url(settings, tenant_key=tenant_key)),
    }
