"""Candidate cache keyed by question, schema and policy (P4-F11).

Only *candidates* are cached — the SQL, its rationale and its assumptions — never
results. A result is data, and a cached count is a wrong count the moment a row
changes; the execution path already re-validates and re-runs every turn.

The cache key is the safety mechanism, not the TTL:

* the **normalised question**, so "  How many rows? " and "how many rows?" are one
  entry;
* the **slice fingerprint**, which already covers the dataset list, each dataset's
  definition hash, and each dataset's row-level policy hash — so a schema change or
  a policy change cannot reuse a statement produced under the old rules;
* the **dialect** and the **row cap**, because both change the SQL;
* a **cache version** constant, so a change to this module's own semantics
  invalidates every entry without a migration.

A refused or invalid answer is never stored: a cache must not make a refusal stick.
Redis is best-effort — an unreachable cache is a miss, not an error — because an
optimisation must never become an outage.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from xp_prompt.config import Settings
from xp_prompt.schema_slice import SchemaSlice

logger = logging.getLogger(__name__)

#: Bump to invalidate every entry after changing what a stored candidate means.
CACHE_VERSION = "1"
CACHE_PREFIX = "xp:sqlcand"
#: Bounded value: a candidate is a statement plus a short rationale.
MAX_VALUE_CHARS = 32_000
DEFAULT_TTL_SECONDS = 1_800
MAX_TTL_SECONDS = 86_400
MAX_QUESTION_CHARS = 2_000

HIT = "hit"
MISS = "miss"
BYPASS = "bypass"
DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class CachedCandidate:
    """A stored candidate, replayed. ``provider``/``model`` name who produced it."""

    sql: str
    dialect: str
    rationale: str
    assumptions: tuple[str, ...]
    tables: tuple[str, ...]
    limit: int
    fingerprint: str
    provider: str
    model: str
    correlation_id: str
    stored_at: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "version": CACHE_VERSION,
            "sql": self.sql,
            "dialect": self.dialect,
            "rationale": self.rationale,
            "assumptions": list(self.assumptions),
            "tables": list(self.tables),
            "limit": self.limit,
            "fingerprint": self.fingerprint,
            "provider": self.provider,
            "model": self.model,
            "correlation_id": self.correlation_id,
            "stored_at": self.stored_at,
        }

    @classmethod
    def from_payload(cls, raw: Any) -> "CachedCandidate | None":
        """Never trust a cache entry: a stale or hand-edited value is a miss."""
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return None
        sql = str(raw.get("sql") or "").strip()
        dialect = str(raw.get("dialect") or "").strip().lower()
        if not sql or not dialect:
            return None
        try:
            limit = int(raw.get("limit") or 0)
        except (TypeError, ValueError):
            return None
        return cls(
            sql=sql,
            dialect=dialect,
            rationale=str(raw.get("rationale") or ""),
            assumptions=tuple(str(item) for item in raw.get("assumptions") or ()),
            tables=tuple(str(item) for item in raw.get("tables") or ()),
            limit=limit,
            fingerprint=str(raw.get("fingerprint") or ""),
            provider=str(raw.get("provider") or ""),
            model=str(raw.get("model") or ""),
            correlation_id=str(raw.get("correlation_id") or ""),
            stored_at=str(raw.get("stored_at") or ""),
        )


def cache_key(
    *,
    tenant_key: str,
    question: str,
    schema: SchemaSlice,
    dialect: str,
    max_rows: int,
) -> str:
    """A stable key over everything that can change a candidate."""
    material = json.dumps(
        {
            "version": CACHE_VERSION,
            "tenant": tenant_key,
            "question": " ".join((question or "").lower().split())[:MAX_QUESTION_CHARS],
            "fingerprint": schema.fingerprint,
            "dialect": (dialect or "").strip().lower(),
            "max_rows": int(max_rows),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{CACHE_PREFIX}:{tenant_key}:{digest}"


def ttl_seconds(settings: Settings) -> int:
    configured = getattr(settings, "candidate_cache_ttl_seconds", DEFAULT_TTL_SECONDS)
    try:
        ttl = int(configured)
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS
    return max(0, min(ttl, MAX_TTL_SECONDS))


def enabled(settings: Settings) -> bool:
    return bool(getattr(settings, "candidate_cache_enabled", True)) and ttl_seconds(
        settings
    ) > 0


def load(settings: Settings, key: str) -> CachedCandidate | None:
    """A stored candidate, or ``None``. Any failure is a miss, never an error."""
    if not enabled(settings):
        return None
    try:
        client = _client(settings)
        raw = client.get(key)
        if not raw:
            return None
        return CachedCandidate.from_payload(json.loads(raw))
    except Exception as exc:  # pragma: no cover - cache is best-effort
        logger.info("sql_cache outcome=error kind=load reason=%s", type(exc).__name__)
        return None


def store(settings: Settings, key: str, candidate: CachedCandidate) -> bool:
    """Store a candidate. Returns whether it was written; never raises."""
    if not enabled(settings):
        return False
    payload = json.dumps(candidate.to_payload(), separators=(",", ":"))
    if len(payload) > MAX_VALUE_CHARS:
        logger.info("sql_cache outcome=skipped reason=too_large")
        return False
    try:
        client = _client(settings)
        client.set(key, payload, ex=ttl_seconds(settings))
        return True
    except Exception as exc:  # pragma: no cover - cache is best-effort
        logger.info("sql_cache outcome=error kind=store reason=%s", type(exc).__name__)
        return False


def _client(settings: Settings) -> Any:
    import redis

    return redis.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        db=settings.redis_database,
        socket_connect_timeout=settings.dependency_timeout_seconds,
        socket_timeout=settings.dependency_timeout_seconds,
        decode_responses=True,
    )
