"""Request rate limits and response headers (P11-F10).

Two hardening measures that belong to the service rather than to the proxy:

* **a rate limit per caller**, because the expensive endpoints spend money: a
  runaway client loops on chat or SQL generation and the provider bill follows.
  The counter is a fixed window in Redis keyed on the authenticated principal, so
  it is shared across gunicorn workers — a per-process counter would multiply the
  limit by the number of workers, which is the same mistake the SQL slot counter
  used to make.
* **response headers**, because these endpoints return JSON that must never be
  cached by a proxy or sniffed into HTML, and the workspace must not be framed.

A Redis outage fails **open**: refusing every AI request because the limiter is
unavailable would turn a cache problem into an outage, and the failure is logged so
it is not silent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from xp_prompt import cache
from xp_prompt.config import Settings

logger = logging.getLogger(__name__)

#: Namespaced beside the statement cache; both live in the same Redis.
KEY_PREFIX = "xp:rl"

#: Headers every API response carries.
SECURITY_HEADERS = {
    # This API only ever returns JSON: nothing here should be sniffed into HTML.
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    # A response can contain a prompt or a statement preview, so no shared cache
    # may keep it.
    "Cache-Control": "no-store",
}


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether a request may proceed, and when to retry if not."""

    allowed: bool
    limit: int
    remaining: int
    retry_after: int = 0

    def to_public_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "limit": self.limit,
            "remaining": self.remaining,
            "retry_after": self.retry_after,
        }


def _client(settings: Settings) -> object:
    return cache._client(settings)  # noqa: SLF001 - one Redis factory, one place


def check(
    settings: Settings,
    *,
    subject: str,
    bucket: str = "ai",
    window_seconds: int = 60,
) -> Decision:
    """Count one request for this subject in this window.

    ``subject`` is the authenticated principal and the bucket, so one noisy user
    cannot exhaust another's allowance. ``limit=0`` disables the check.
    """
    limit = int(getattr(settings, "rate_limit_per_minute", 0) or 0)
    if limit <= 0 or not subject:
        return Decision(True, 0, 0)
    key = f"{KEY_PREFIX}:{bucket}:{subject}:{window_seconds}"
    try:
        client = _client(settings)
        used = int(client.incr(key))
        if used == 1:
            client.expire(key, window_seconds)
        if used > limit:
            logger.warning(
                "rate_limited bucket=%s subject=%s used=%d limit=%d",
                bucket,
                subject[-12:],
                used,
                limit,
            )
            return Decision(False, limit, 0, retry_after=window_seconds)
        return Decision(True, limit, max(0, limit - used))
    except Exception as exc:  # noqa: BLE001 - a broken limiter must not stop work
        logger.warning(
            "rate_limit_unavailable bucket=%s reason=%s", bucket, type(exc).__name__
        )
        return Decision(True, limit, limit)
