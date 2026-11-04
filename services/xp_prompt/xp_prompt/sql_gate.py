"""Deployment-wide execution slots for AI-issued statements (P10-F06).

``sql.max_concurrent`` says how many statements the *deployment* may run at once.
A counter in one process cannot express that: Superset runs several worker
processes, so a per-process semaphore multiplies the limit by the number of
workers. The counter therefore lives in Redis, which every worker already shares,
and it is incremented and decremented atomically.

The service owns the number and the bookkeeping; the executor asks for a slot
before it starts a query and gives it back when the query ends. A slot also
carries a TTL — a worker that dies mid-query must not hold a slot forever, and an
expired slot is a smaller problem than a deployment that answers 429 until someone
notices.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from xp_prompt import cache, settings_registry
from xp_prompt.config import Settings

logger = logging.getLogger(__name__)

#: Namespaced like the statement cache, so the deployment can share one Redis.
KEY = "xp_prompt:sql:slots"

#: A slot outlives a query by a wide margin, but not forever: the timeout is the
#: upper bound of legitimate work, and this is the backstop for a crash.
TTL_MULTIPLIER = 2
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 3_600


class Slot:
    """One held execution slot: its token, and the limits it was taken under."""

    def __init__(self, token: str, *, limit: int, ttl_seconds: int) -> None:
        self.token = token
        self.limit = limit
        self.ttl_seconds = ttl_seconds

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "limit": self.limit,
            "ttl_seconds": self.ttl_seconds,
        }


def _client(settings: Settings) -> Any:
    return cache._client(settings)  # noqa: SLF001 - one Redis factory, one place


def _ttl_seconds(timeout_ms: int) -> int:
    seconds = max(1, int(timeout_ms)) // 1000 * TTL_MULTIPLIER
    return max(MIN_TTL_SECONDS, min(seconds, MAX_TTL_SECONDS))


def acquire(
    settings: Settings, *, tenant_key: str = "default"
) -> Slot | None:
    """Take one slot, or answer ``None`` when the deployment is saturated.

    ``None`` is a refusal to start, not a failure: queueing would only move the
    pressure somewhere the caller cannot see it.
    """
    limits = settings_registry.sql_limits(settings, tenant_key=tenant_key)
    ttl = _ttl_seconds(limits.timeout_ms)
    try:
        client = _client(settings)
        current = int(client.incr(KEY))
        if current == 1:
            client.expire(KEY, ttl)
        if current > limits.max_concurrent:
            client.decr(KEY)
            logger.info(
                "sql_slot outcome=refused active=%d limit=%d",
                current - 1,
                limits.max_concurrent,
            )
            return None
        token = uuid.uuid4().hex
        client.hset(f"{KEY}:{token}", mapping={"active": "1"})
        client.expire(f"{KEY}:{token}", ttl)
        return Slot(token, limit=limits.max_concurrent, ttl_seconds=ttl)
    except Exception as exc:  # noqa: BLE001 - a broken counter must not block SQL
        logger.warning(
            "sql_slot outcome=unavailable reason=%s", type(exc).__name__, exc_info=True
        )
        return Slot("unmetered", limit=limits.max_concurrent, ttl_seconds=ttl)


def release(settings: Settings, token: str) -> bool:
    """Give a slot back. Releasing twice is harmless; an unknown token is a no-op."""
    if not token or token == "unmetered":
        return False
    try:
        client = _client(settings)
        if client.delete(f"{KEY}:{token}"):
            remaining = int(client.decr(KEY) or 0)
            if remaining < 0:
                # A crashed worker or a double release must not leave the counter
                # negative, which would hand out slots it never had.
                client.set(KEY, 0)
            return True
        return False
    except Exception as exc:  # noqa: BLE001 - release is best-effort
        logger.warning(
            "sql_slot_release outcome=unavailable reason=%s", type(exc).__name__
        )
        return False


def active(settings: Settings) -> int:
    """How many slots the deployment is currently using, for the admin view."""
    try:
        return max(0, int(_client(settings).get(KEY) or 0))
    except Exception:  # noqa: BLE001
        return 0
