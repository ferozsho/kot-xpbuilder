"""Validated repair patterns: the only fixes the engine may reuse (P5-F02/F03/F08/F09).

A repair engine that remembers nothing is just a retry loop. A repair engine that
remembers *everything* becomes a way for model output to silently become policy. This
module is the narrow middle: a pattern is a correction for one identified failure, and
it may only be offered again after it has been validated, twice over, in this tenant.

The rules, in order of importance:

* **a pattern never crosses a tenant.** Every read and write is filtered by the tenant
  row id. There is no shared pool, so there is nothing to leak from.
* **a pattern never crosses a schema or a dialect.** It is stored with the failure
  fingerprint *and* the slice fingerprint; a changed schema, or a changed row-level
  policy, produces a different fingerprint and the old pattern simply stops matching.
* **nothing is offered until it is promoted.** ``candidate → validating → validated →
  promoted``; search returns ``promoted`` only. Promotion needs the configured evidence
  threshold, met by *distinct* validations, not by one conversation retrying itself.
* **bad knowledge is disabled immediately.** ``deprecated`` and ``quarantined`` both
  leave search at once, a superseded version can be restored, and a stale validator or
  policy version sends a pattern back to revalidation rather than keeping its standing.
* **the correction is sealed.** Like every other statement (ADR-0016) it is stored with
  :class:`SecretBox` and identified by a SHA-256. Operator listings never contain it.
* **retrieval is exact-match, deliberately.** There is no embedding provider in this
  deployment, and exact scope matching is also the *degraded* mode the Phase 5 plan
  requires when Qdrant is unavailable — so this path is always the safe one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from xp_prompt.config import Settings
from xp_prompt.db import connect
from xp_prompt.security import SecretBox

logger = logging.getLogger(__name__)

#: Every state a pattern may be in. ``promoted`` is the only usable one.
STATES: tuple[str, ...] = (
    "candidate",
    "validating",
    "validated",
    "promoted",
    "deprecated",
    "quarantined",
)
#: States a search may return.
USABLE_STATES: tuple[str, ...] = ("promoted",)
#: Operator-initiated transitions (``restore`` is the rollback).
ACTIONS: tuple[str, ...] = ("promote", "deprecate", "quarantine", "restore")

#: Bump when the guard, the semantic check or the result contract changes shape: a
#: pattern proven against an older validator has to earn its promotion again.
VALIDATOR_VERSION = "p4.2026-11"
#: Bump when the policy surface changes (rendered predicates, allowed shapes).
POLICY_VERSION = "1"

#: How many independent successful validations a pattern needs before promotion.
DEFAULT_EVIDENCE_THRESHOLD = 2
#: Below this, a pattern is not trustworthy even if the count is met.
MIN_CONFIDENCE = 0.6
#: Distinct validations kept per pattern, so evidence stays a small list.
MAX_EVIDENCE = 20
#: Recency half-life for ranking: a year-old success is worth less than a new one.
RECENCY_HALF_LIFE_DAYS = 90.0

MAX_DIALECT_CHARS = 32
MAX_INTENT_CHARS = 32
MAX_CORRECTION_CHARS = 20_000
MAX_TABLES = 20
MAX_REASON_CHARS = 64
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 100


class PatternError(ValueError):
    """A refused pattern transition (safe to return to the caller)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Pattern:
    """One stored correction for one identified failure, with its standing."""

    id: str
    state: str
    version: int
    dialect: str
    kind: str
    failure_fingerprint: str
    schema_fingerprint: str
    intent: str
    correction_sha256: str
    successes: int = 0
    failures: int = 0
    confidence: float = 0.0
    origin: str = "generated"
    promotion_policy: str = ""
    validator_version: str = ""
    policy_version: str = ""
    superseded_by: str | None = None
    reason: str = ""
    tables: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_used_at: datetime | None = None
    #: Filled in by :func:`rank`; never stored.
    score: float = 0.0
    factors: dict[str, float] = field(default_factory=dict)
    #: Only populated when a caller explicitly asks for the statement.
    sql: str | None = field(default=None, repr=False)

    def to_public_dict(self, *, include_sql: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "state": self.state,
            "version": self.version,
            "dialect": self.dialect,
            "kind": self.kind,
            "failure_fingerprint": self.failure_fingerprint,
            "schema_fingerprint": self.schema_fingerprint,
            "intent": self.intent,
            "correction_sha256": self.correction_sha256,
            "successes": self.successes,
            "failures": self.failures,
            "confidence": self.confidence,
            "origin": self.origin,
            "promotion_policy": self.promotion_policy,
            "validator_version": self.validator_version,
            "policy_version": self.policy_version,
            "superseded_by": self.superseded_by,
            "reason": self.reason,
            "tables": list(self.tables),
            "evidence_count": len(self.evidence),
            "created_at": _isoformat(self.created_at),
            "updated_at": _isoformat(self.updated_at),
            "last_used_at": _isoformat(self.last_used_at),
            "sql_hidden": self.sql is None or not include_sql,
        }
        if include_sql and self.sql is not None:
            payload["sql"] = self.sql
        if self.score or self.factors:
            payload["score"] = round(self.score, 4)
            payload["factors"] = {key: round(value, 4) for key, value in self.factors.items()}
        return payload


def confidence(successes: int, failures: int) -> float:
    """Laplace-smoothed success rate: 0 successes is not 0 confidence, it is unknown."""
    good = max(0, int(successes))
    bad = max(0, int(failures))
    return round((good + 1) / (good + bad + 2), 4)


def correction_sha256(sql: str) -> str:
    """Identity of a correction, normalized enough that spacing is not a new pattern."""
    text = " ".join((sql or "").split())
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""


def record_candidate(
    settings: Settings,
    *,
    tenant_key: str,
    failure_fingerprint: str,
    kind: str,
    stage: str,
    dialect: str,
    schema_fingerprint: str,
    sql: str,
    intent: str = "query",
    origin: str = "generated",
    tables: Any = None,
) -> str | None:
    """Store (or find) a correction that has not yet earned promotion.

    Returns the pattern id, or ``None`` when the correction is unusable. Never
    raises: a broken pattern store degrades reuse, not the turn.
    """
    statement = (sql or "").strip()
    digest = correction_sha256(statement)
    if not digest or not failure_fingerprint or len(statement) > MAX_CORRECTION_CHARS:
        return None
    sealed = SecretBox.from_root_secret(settings.service_secret).encrypt(
        statement
    ).encode("utf-8")
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                tenant_id = _tenant_id(cursor, tenant_key)
                if tenant_id is None:
                    return None
                cursor.execute(
                    """
                    INSERT INTO ai_repair_pattern (
                        tenant_id, state, kind, stage, failure_fingerprint, dialect,
                        schema_fingerprint, intent, correction_ciphertext,
                        correction_sha256, tables, origin, validator_version,
                        policy_version
                    )
                    VALUES (%s, 'candidate', %s, %s, %s, %s, %s, %s, %s, %s,
                            %s::jsonb, %s, %s, %s)
                    ON CONFLICT (
                        tenant_id, failure_fingerprint, dialect,
                        schema_fingerprint, intent, correction_sha256
                    ) DO UPDATE SET updated_at = CURRENT_TIMESTAMP
                    RETURNING id, state
                    """,
                    (
                        tenant_id,
                        (kind or "")[:32],
                        (stage or "")[:24],
                        (failure_fingerprint or "")[:64],
                        (dialect or "").strip().lower()[:MAX_DIALECT_CHARS],
                        (schema_fingerprint or "")[:64],
                        (intent or "")[:MAX_INTENT_CHARS],
                        sealed,
                        digest,
                        _json_list(tables),
                        (origin or "generated")[:16],
                        VALIDATOR_VERSION,
                        POLICY_VERSION,
                    ),
                )
                row = cursor.fetchone()
        return str(row["id"]) if row else None
    except Exception as exc:  # pragma: no cover - best effort
        logger.info(
            "repair_pattern outcome=error kind=record reason=%s", type(exc).__name__
        )
        return None


def record_validation(
    settings: Settings,
    *,
    tenant_key: str,
    pattern_id: str,
    succeeded: bool,
    evidence: str = "",
    threshold: int = DEFAULT_EVIDENCE_THRESHOLD,
) -> dict[str, Any] | None:
    """Record one validation outcome and advance the pattern's state.

    ``evidence`` names the independent validator (a conversation id, or
    ``operator``): promotion counts *distinct* evidence, so one conversation cannot
    promote a fix by succeeding with it repeatedly.
    """
    source = (evidence or "").strip()[:64]
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                tenant_id = _tenant_id(cursor, tenant_key)
                if tenant_id is None:
                    return None
                cursor.execute(
                    """
                    SELECT id, state, successes, failures, evidence
                    FROM ai_repair_pattern
                    WHERE tenant_id = %s AND id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, pattern_id),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                current_state = str(row["state"])
                if current_state in {"deprecated", "quarantined"}:
                    # Disabled knowledge stays disabled: a stray success must not
                    # quietly resurrect something an operator switched off.
                    return {
                        "id": str(row["id"]),
                        "state": current_state,
                        "successes": int(row["successes"]),
                        "failures": int(row["failures"]),
                        "confidence": confidence(row["successes"], row["failures"]),
                        "promoted": False,
                    }
                successes = int(row["successes"]) + (1 if succeeded else 0)
                failures = int(row["failures"]) + (0 if succeeded else 1)
                sources = _evidence(row["evidence"])
                if succeeded and source and source not in sources:
                    sources.append(source)
                sources = sources[:MAX_EVIDENCE]
                score = confidence(successes, failures)
                state = _settled_state(
                    current_state, successes, failures, sources, threshold
                )
                cursor.execute(
                    """
                    UPDATE ai_repair_pattern
                    SET successes = %s, failures = %s, confidence = %s, state = %s,
                        evidence = %s::jsonb, last_used_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (
                        successes,
                        failures,
                        score,
                        state,
                        json.dumps(sources),
                        tenant_id,
                        pattern_id,
                    ),
                )
        logger.info(
            "repair_pattern outcome=validated state=%s successes=%d failures=%d",
            state,
            successes,
            failures,
        )
        return {
            "id": str(row["id"]),
            "state": state,
            "successes": successes,
            "failures": failures,
            "confidence": score,
            "promoted": state == "promoted",
        }
    except Exception as exc:  # pragma: no cover - best effort
        logger.info(
            "repair_pattern outcome=error kind=validate reason=%s", type(exc).__name__
        )
        return None


def promote(
    settings: Settings,
    *,
    tenant_key: str,
    pattern_id: str,
    threshold: int = DEFAULT_EVIDENCE_THRESHOLD,
    force: bool = False,
) -> dict[str, Any]:
    """Promote a validated pattern, superseding the version it replaces.

    Without ``force`` the evidence threshold and the confidence floor are enforced
    here; ``force`` is the operator override, and it still refuses a pattern that has
    never succeeded. Either way the previous promoted correction for the same scope is
    deprecated, so "the current answer" is always one row.
    """
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                tenant_id = _tenant_id(cursor, tenant_key)
                if tenant_id is None:
                    raise PatternError("not_found", "tenant not found")
                cursor.execute(
                    """
                    SELECT id, state, successes, failures, evidence, dialect,
                           failure_fingerprint, schema_fingerprint, intent
                    FROM ai_repair_pattern
                    WHERE tenant_id = %s AND id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, pattern_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise PatternError("not_found", "pattern not found")
                if row["state"] in {"deprecated", "quarantined"}:
                    raise PatternError(
                        "disabled", "a disabled pattern must be restored, not promoted"
                    )
                successes = int(row["successes"])
                sources = _evidence(row["evidence"])
                score = confidence(successes, row["failures"])
                if successes < 1:
                    raise PatternError(
                        "unvalidated", "a pattern must succeed once before promotion"
                    )
                if not force:
                    if successes < max(1, threshold):
                        raise PatternError(
                            "below_threshold",
                            f"needs {max(1, threshold)} successful validations",
                        )
                    if len(sources) < max(1, threshold):
                        raise PatternError(
                            "insufficient_evidence",
                            "validations must come from independent sources",
                        )
                    if score < MIN_CONFIDENCE:
                        raise PatternError("low_confidence", "confidence is too low")
                cursor.execute(
                    """
                    SELECT COALESCE(MAX(version), 0) AS version
                    FROM ai_repair_pattern
                    WHERE tenant_id = %s AND failure_fingerprint = %s
                      AND dialect = %s AND schema_fingerprint = %s AND intent = %s
                    """,
                    (
                        tenant_id,
                        row["failure_fingerprint"],
                        row["dialect"],
                        row["schema_fingerprint"],
                        row["intent"],
                    ),
                )
                version_row = cursor.fetchone()
                version = int((version_row or {}).get("version") or 0) + 1
                cursor.execute(
                    """
                    UPDATE ai_repair_pattern
                    SET state = 'deprecated',
                        superseded_by = %s,
                        reason = 'superseded',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND state = 'promoted'
                      AND id <> %s
                      AND failure_fingerprint = %s AND dialect = %s
                      AND schema_fingerprint = %s AND intent = %s
                    """,
                    (
                        pattern_id,
                        tenant_id,
                        pattern_id,
                        row["failure_fingerprint"],
                        row["dialect"],
                        row["schema_fingerprint"],
                        row["intent"],
                    ),
                )
                cursor.execute(
                    """
                    UPDATE ai_repair_pattern
                    SET state = 'promoted', version = %s,
                        promotion_policy = %s, reason = '',
                        validator_version = %s, policy_version = %s,
                        superseded_by = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (
                        version,
                        "operator" if force else "automatic",
                        VALIDATOR_VERSION,
                        POLICY_VERSION,
                        tenant_id,
                        pattern_id,
                    ),
                )
        logger.info("repair_pattern outcome=promoted version=%d", version)
        return {
            "id": pattern_id,
            "state": "promoted",
            "version": version,
            "promotion_policy": "operator" if force else "automatic",
            "confidence": score,
            "successes": successes,
            "evidence_count": len(sources),
        }
    except PatternError:
        raise
    except Exception as exc:
        raise PatternError("store_unavailable", "the pattern store is unavailable") from exc


def set_state(
    settings: Settings, *, tenant_key: str, pattern_id: str, action: str, reason: str = ""
) -> dict[str, Any]:
    """Deprecate, quarantine, or restore a pattern (rollback), immediately.

    ``quarantine`` is the emergency brake: whatever state the pattern is in, it stops
    being offered. ``restore`` puts a superseded correction back — that is the
    rollback path when a newer version turns out to be worse.
    """
    if action not in ACTIONS:
        raise PatternError("unsupported_action", "unknown pattern action")
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                tenant_id = _tenant_id(cursor, tenant_key)
                if tenant_id is None:
                    raise PatternError("not_found", "tenant not found")
                cursor.execute(
                    """
                    SELECT id, state, successes, failures, evidence, version,
                           failure_fingerprint, dialect, schema_fingerprint, intent
                    FROM ai_repair_pattern
                    WHERE tenant_id = %s AND id = %s
                    FOR UPDATE
                    """,
                    (tenant_id, pattern_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise PatternError("not_found", "pattern not found")
                state = str(row["state"])
                note = (reason or "")[:MAX_REASON_CHARS]
                if action == "promote":
                    raise PatternError(
                        "unsupported_action", "promotion has its own endpoint"
                    )
                if action == "deprecate":
                    if state == "deprecated":
                        raise PatternError("unchanged", "already deprecated")
                    new_state = "deprecated"
                elif action == "quarantine":
                    if state == "quarantined":
                        raise PatternError("unchanged", "already quarantined")
                    new_state = "quarantined"
                else:  # restore
                    if state not in {"deprecated", "quarantined"}:
                        raise PatternError(
                            "not_disabled", "only a disabled pattern can be restored"
                        )
                    if int(row["successes"]) < 1:
                        raise PatternError(
                            "unvalidated", "a pattern must succeed once before restore"
                        )
                    new_state = "promoted" if int(row["version"]) else "validated"
                if new_state == "promoted":
                    # A restore is a rollback: the version being brought back becomes
                    # the current one, so whatever superseded it steps aside.
                    cursor.execute(
                        """
                        UPDATE ai_repair_pattern
                        SET state = 'deprecated', superseded_by = %s,
                            reason = 'superseded', updated_at = CURRENT_TIMESTAMP
                        WHERE tenant_id = %s AND state = 'promoted' AND id <> %s
                          AND failure_fingerprint = %s AND dialect = %s
                          AND schema_fingerprint = %s AND intent = %s
                        """,
                        (
                            pattern_id,
                            tenant_id,
                            pattern_id,
                            row["failure_fingerprint"],
                            row["dialect"],
                            row["schema_fingerprint"],
                            row["intent"],
                        ),
                    )
                cursor.execute(
                    """
                    UPDATE ai_repair_pattern
                    SET state = %s, reason = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND id = %s
                    """,
                    (new_state, note, tenant_id, pattern_id),
                )
        logger.info("repair_pattern outcome=state_changed state=%s action=%s", new_state, action)
        return {
            "id": pattern_id,
            "state": new_state,
            "action": action,
            "reason": note,
        }
    except PatternError:
        raise
    except Exception as exc:
        raise PatternError("store_unavailable", "the pattern store is unavailable") from exc


def find(
    settings: Settings,
    *,
    tenant_key: str,
    failure_fingerprint: str,
    dialect: str,
    schema_fingerprint: str,
    intent: str = "query",
    exclude: Any = None,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[Pattern]:
    """Promoted corrections for exactly this failure, in this tenant and scope.

    Every part of that sentence is a filter: tenant, state, dialect, schema/policy
    fingerprint, failure fingerprint, intent. A miss is the normal, safe outcome.
    """
    excluded = {str(item) for item in (exclude or ()) if item}
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                tenant_id = _tenant_id(cursor, tenant_key)
                if tenant_id is None:
                    return []
                cursor.execute(
                    """
                    SELECT *
                    FROM ai_repair_pattern
                    WHERE tenant_id = %s AND state = 'promoted'
                      AND failure_fingerprint = %s AND dialect = %s
                      AND schema_fingerprint = %s
                      AND (intent = %s OR intent = '')
                    ORDER BY confidence DESC, successes DESC, updated_at DESC
                    LIMIT %s
                    """,
                    (
                        tenant_id,
                        (failure_fingerprint or "")[:64],
                        (dialect or "").strip().lower(),
                        (schema_fingerprint or "")[:64],
                        (intent or "")[:MAX_INTENT_CHARS],
                        max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT)),
                    ),
                )
                rows = cursor.fetchall()
    except Exception as exc:  # pragma: no cover - best effort
        logger.info("repair_pattern outcome=error kind=find reason=%s", type(exc).__name__)
        return []
    found = [
        _pattern(settings, row, include_sql=True)
        for row in rows
        if row["correction_sha256"] not in excluded
    ]
    return rank([pattern for pattern in found if pattern is not None])


def rank(patterns_found: list[Pattern], *, now: datetime | None = None) -> list[Pattern]:
    """Order candidates by validation history, recency, confidence, compatibility.

    The score is a weighted sum of four explainable factors rather than a model, so
    the operator view can say *why* one correction won (P5-F10).
    """
    moment = now or datetime.now(UTC)
    ranked: list[Pattern] = []
    for pattern in patterns_found:
        rate = pattern.confidence or confidence(pattern.successes, pattern.failures)
        total = pattern.successes + pattern.failures
        history = pattern.successes / (total + 1)
        recency = _recency(pattern, moment)
        compatible = 1.0 if pattern.validator_version == VALIDATOR_VERSION else 0.0
        if pattern.policy_version != POLICY_VERSION:
            compatible = 0.0
        score = 0.45 * rate + 0.30 * history + 0.15 * recency + 0.10 * compatible
        ranked.append(
            replace(
                pattern,
                score=score,
                factors={
                    "confidence": rate,
                    "history": history,
                    "recency": recency,
                    "compatibility": compatible,
                },
            )
        )
    ranked.sort(key=lambda pattern: pattern.score, reverse=True)
    return ranked


def list_patterns(
    settings: Settings,
    *,
    tenant_key: str,
    state: str = "",
    failure_fingerprint: str = "",
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[Pattern]:
    """Operator view: the stored knowledge, without anyone's statement in it."""
    size = max(1, min(int(limit or DEFAULT_LIST_LIMIT), MAX_LIST_LIMIT))
    wanted = (state or "").strip().lower()
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                tenant_id = _tenant_id(cursor, tenant_key)
                if tenant_id is None:
                    return []
                if wanted in STATES:
                    cursor.execute(
                        """
                        SELECT * FROM ai_repair_pattern
                        WHERE tenant_id = %s AND state = %s
                        ORDER BY updated_at DESC
                        LIMIT %s
                        """,
                        (tenant_id, wanted, size),
                    )
                elif failure_fingerprint:
                    cursor.execute(
                        """
                        SELECT * FROM ai_repair_pattern
                        WHERE tenant_id = %s AND failure_fingerprint = %s
                        ORDER BY version DESC, updated_at DESC
                        LIMIT %s
                        """,
                        (tenant_id, failure_fingerprint[:64], size),
                    )
                else:
                    cursor.execute(
                        """
                        SELECT * FROM ai_repair_pattern
                        WHERE tenant_id = %s
                        ORDER BY updated_at DESC
                        LIMIT %s
                        """,
                        (tenant_id, size),
                    )
                rows = cursor.fetchall()
    except Exception as exc:  # pragma: no cover - best effort
        logger.info("repair_pattern outcome=error kind=list reason=%s", type(exc).__name__)
        return []
    return [
        pattern
        for pattern in (_pattern(settings, row) for row in rows)
        if pattern is not None
    ]


def metrics(settings: Settings, *, tenant_key: str) -> dict[str, Any]:
    """Counts an operator needs, with no statement and no question in them (P5-F10)."""
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                tenant_id = _tenant_id(cursor, tenant_key)
                if tenant_id is None:
                    return _empty_metrics()
                cursor.execute(
                    """
                    SELECT state, COUNT(*) AS total,
                           COALESCE(SUM(successes), 0) AS successes,
                           COALESCE(SUM(failures), 0) AS failures
                    FROM ai_repair_pattern
                    WHERE tenant_id = %s
                    GROUP BY state
                    """,
                    (tenant_id,),
                )
                by_state = {str(row["state"]): row for row in cursor.fetchall()}
                cursor.execute(
                    """
                    SELECT outcome, COUNT(*) AS total
                    FROM ai_repair_attempt
                    WHERE tenant_id = %s
                    GROUP BY outcome
                    """,
                    (tenant_id,),
                )
                by_outcome = {str(row["outcome"]): int(row["total"]) for row in cursor.fetchall()}
                cursor.execute(
                    """
                    SELECT kind, COUNT(*) AS total
                    FROM ai_repair_attempt
                    WHERE tenant_id = %s
                    GROUP BY kind ORDER BY total DESC LIMIT 10
                    """,
                    (tenant_id,),
                )
                by_kind = [
                    {"kind": str(row["kind"]), "total": int(row["total"])}
                    for row in cursor.fetchall()
                ]
    except Exception as exc:  # pragma: no cover - best effort
        logger.info("repair_pattern outcome=error kind=metrics reason=%s", type(exc).__name__)
        return _empty_metrics()

    states = {state: int(by_state.get(state, {}).get("total") or 0) for state in STATES}
    attempted = sum(by_outcome.get(name, 0) for name in ("attempted", "succeeded", "failed"))
    succeeded = by_outcome.get("succeeded", 0)
    rate = round(succeeded / attempted, 4) if attempted else 0.0
    return {
        "states": states,
        "attempts": {
            "attempted": by_outcome.get("attempted", 0),
            "succeeded": succeeded,
            "failed": by_outcome.get("failed", 0),
            "suppressed": by_outcome.get("suppressed", 0),
        },
        "success_rate": rate,
        "kinds": by_kind,
        "usable": states.get("promoted", 0),
        "disabled": states.get("deprecated", 0) + states.get("quarantined", 0),
    }


def revalidate_stale(settings: Settings, *, tenant_key: str) -> int:
    """Send patterns proven by an older validator or policy back to validation.

    This is the "a policy change must invalidate affected patterns" rule: the pattern
    is not deleted, it just has to earn promotion again under the current rules.
    """
    try:
        with connect(settings) as connection:
            with connection.cursor() as cursor:
                tenant_id = _tenant_id(cursor, tenant_key)
                if tenant_id is None:
                    return 0
                cursor.execute(
                    """
                    UPDATE ai_repair_pattern
                    SET state = 'validated', reason = 'stale_validator',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE tenant_id = %s AND state = 'promoted'
                      AND (validator_version <> %s OR policy_version <> %s)
                    """,
                    (tenant_id, VALIDATOR_VERSION, POLICY_VERSION),
                )
                changed = cursor.rowcount
    except Exception as exc:  # pragma: no cover - best effort
        logger.info("repair_pattern outcome=error kind=revalidate reason=%s", type(exc).__name__)
        return 0
    if changed:
        logger.info("repair_pattern outcome=revalidated count=%d", changed)
    return int(changed or 0)


def _settled_state(
    current: str, successes: int, failures: int, sources: list[str], threshold: int
) -> str:
    """Where a pattern stands after one more validation."""
    if current in {"deprecated", "quarantined"}:
        # Knowledge an operator switched off does not come back on its own.
        return current
    if successes < 1:
        return "validating" if current in {"candidate", "validating"} else current
    if current == "promoted":
        # An already-promoted pattern keeps its standing unless it stops being right.
        return "promoted" if confidence(successes, failures) >= MIN_CONFIDENCE else "validated"
    needed = max(1, threshold)
    if successes >= needed and len(sources) >= needed:
        if confidence(successes, failures) >= MIN_CONFIDENCE:
            return "promoted"
    return "validated"


def _recency(pattern: Pattern, moment: datetime) -> float:
    moment_at = pattern.last_used_at or pattern.updated_at or pattern.created_at
    if moment_at is None:
        return 0.5
    if moment_at.tzinfo is None:
        moment_at = moment_at.replace(tzinfo=UTC)
    age_days = max(0.0, (moment - moment_at).total_seconds() / 86_400)
    return round(math.exp(-age_days / RECENCY_HALF_LIFE_DAYS), 4)


def _pattern(
    settings: Settings, row: dict[str, Any], *, include_sql: bool = False
) -> Pattern | None:
    statement: str | None = None
    if include_sql:
        try:
            box = SecretBox.from_root_secret(settings.service_secret)
            statement = box.decrypt(bytes(row["correction_ciphertext"]).decode("utf-8"))
        except Exception:  # pragma: no cover - rotation or tampering
            return None
    return Pattern(
        id=str(row["id"]),
        state=str(row["state"]),
        version=int(row.get("version") or 1),
        dialect=str(row.get("dialect") or ""),
        kind=str(row.get("kind") or ""),
        failure_fingerprint=str(row.get("failure_fingerprint") or ""),
        schema_fingerprint=str(row.get("schema_fingerprint") or ""),
        intent=str(row.get("intent") or ""),
        correction_sha256=str(row.get("correction_sha256") or ""),
        successes=int(row.get("successes") or 0),
        failures=int(row.get("failures") or 0),
        confidence=float(row.get("confidence") or 0.0),
        origin=str(row.get("origin") or ""),
        promotion_policy=str(row.get("promotion_policy") or ""),
        validator_version=str(row.get("validator_version") or ""),
        policy_version=str(row.get("policy_version") or ""),
        superseded_by=str(row["superseded_by"]) if row.get("superseded_by") else None,
        reason=str(row.get("reason") or ""),
        tables=tuple(row.get("tables") or ()),
        evidence=tuple(row.get("evidence") or ()),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        last_used_at=row.get("last_used_at"),
        sql=statement,
    )


def _empty_metrics() -> dict[str, Any]:
    return {
        "states": {state: 0 for state in STATES},
        "attempts": {"attempted": 0, "succeeded": 0, "failed": 0, "suppressed": 0},
        "success_rate": 0.0,
        "kinds": [],
        "usable": 0,
        "disabled": 0,
    }


def _evidence(value: Any) -> list[str]:
    items: list[str] = []
    if isinstance(value, (list, tuple)):
        for entry in value:
            text = str(entry or "").strip()[:64]
            if text and text not in items:
                items.append(text)
    return items


def _tenant_id(cursor: Any, tenant_key: str) -> Any:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    return row["id"] if row else None


def _json_list(value: Any) -> str:
    items: list[str] = []
    if isinstance(value, (list, tuple)):
        for entry in value:
            text = str(entry or "").strip()[:128]
            if text and text not in items:
                items.append(text)
            if len(items) >= MAX_TABLES:
                break
    return json.dumps(items)


def _isoformat(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None
