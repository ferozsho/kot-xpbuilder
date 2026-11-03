"""Provider health policy: passive health state, circuit breaker, backoff.

Two independent concerns live here so adapters stay free of routing policy:

* **Passive health** — every attempt outcome folds into a small, redacted state
  (``consecutive_failures``, ``circuit_open_until``, ``last_error_code``) that is
  persisted on the provider row. No prompt or response body is ever stored.
* **Retry pacing** — bounded exponential backoff with full jitter, honouring a
  provider's ``Retry-After`` hint but never above a hard ceiling, so a rate
  limited provider cannot turn one request into a retry storm.

The state is deliberately timestamp-based rather than counter-based: an open
circuit closes by the clock, so a process restart cannot wedge a provider.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

#: Consecutive retryable failures before a provider is skipped entirely.
FAILURE_THRESHOLD = 3
#: First cool-down window; doubles for each further failure.
OPEN_SECONDS = 60
#: A provider is never parked longer than this, however often it fails.
MAX_OPEN_SECONDS = 900
#: Attempts allowed for one provider inside a single logical request.
MAX_ATTEMPTS_PER_PROVIDER = 2
BASE_RETRY_DELAY_SECONDS = 0.5
MAX_RETRY_DELAY_SECONDS = 8.0

Jitter = Callable[[], float]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Redacted health of one provider row."""

    consecutive_failures: int = 0
    circuit_open_until: datetime | None = None
    last_error_code: str | None = None
    last_failure_at: datetime | None = None

    def is_open(self, now: datetime | None = None) -> bool:
        """Is the breaker currently skipping this provider?"""
        if self.circuit_open_until is None:
            return False
        return self.circuit_open_until > (now or utcnow())

    def open_seconds(self) -> int:
        """Cool-down for the next trip, growing with repeated failures."""
        if self.consecutive_failures < FAILURE_THRESHOLD:
            return 0
        exponent = min(self.consecutive_failures - FAILURE_THRESHOLD, 8)
        return min(OPEN_SECONDS * (2**exponent), MAX_OPEN_SECONDS)

    def to_public_dict(self, now: datetime | None = None) -> dict[str, object]:
        moment = now or utcnow()
        return {
            "state": "open" if self.is_open(moment) else "closed",
            "consecutive_failures": self.consecutive_failures,
            "open_until": _isoformat(self.circuit_open_until),
            "last_error": self.last_error_code,
            "last_failure_at": _isoformat(self.last_failure_at),
        }


def after_failure(
    health: ProviderHealth | None,
    *,
    error_code: str,
    retryable: bool,
    now: datetime | None = None,
) -> ProviderHealth:
    """Fold one failed attempt into the provider health state.

    A non-retryable failure (bad credentials, rejected request) still counts
    toward the breaker so a misconfigured provider stops consuming attempts,
    but it never extends an already-advanced cool-down window by itself.
    """
    current = health or ProviderHealth()
    moment = now or utcnow()
    failures = current.consecutive_failures + 1
    next_state = replace(
        current,
        consecutive_failures=failures,
        last_error_code=error_code,
        last_failure_at=moment,
    )
    if failures < FAILURE_THRESHOLD:
        return next_state
    return replace(
        next_state,
        circuit_open_until=moment + timedelta(seconds=next_state.open_seconds()),
    )


def after_success() -> ProviderHealth:
    """A successful call clears the failure streak and closes the breaker."""
    return ProviderHealth()


def retry_delay_seconds(
    attempt: int,
    *,
    retry_after_seconds: int | None = None,
    jitter: Jitter = random.random,
) -> float:
    """Delay before the next attempt of the *same* provider.

    ``attempt`` is 1-based. Full jitter (``random() * ceiling``) spreads
    concurrent callers that all hit the same rate limit.
    """
    if retry_after_seconds is not None and retry_after_seconds > 0:
        return float(min(retry_after_seconds, MAX_RETRY_DELAY_SECONDS))
    exponent = max(attempt - 1, 0)
    ceiling = min(BASE_RETRY_DELAY_SECONDS * (2**exponent), MAX_RETRY_DELAY_SECONDS)
    return round(max(0.0, min(jitter(), 1.0)) * ceiling, 3)


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
