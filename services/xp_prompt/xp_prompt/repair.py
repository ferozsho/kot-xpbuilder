"""The repair decision: reuse validated knowledge, or ask for exactly one correction.

This is the module the plan's reuse rule points at — *one* repair contract, owned by the
service, that every caller reports failures to. Its whole job is to answer a narrow
question: **is there a fix for this failure that policy allows me to try, and is it one
we have not already watched fail?**

The order of preference is the safety property:

1. **a promoted pattern**, matched on tenant, dialect, schema/policy fingerprint, failure
   fingerprint and intent (P5-F02), ranked by validation history, recency, confidence and
   compatibility (P5-F03). Reusing a fix that already worked beats inventing another.
2. **one generated correction**, and only when no safe pattern applies (P5-F04). It is
   asked for with the failed statement and the failure in the prompt, with the candidate
   cache bypassed, because the cached statement *is* the one that failed.
3. **nothing.** A terminal policy outcome, an exhausted budget, or a correction whose
   fingerprint is already in this failure's failed-fix set (P5-F06/F07) all end here.

Whatever is proposed then goes through :func:`sql_execution.validate` — the same guard,
the same semantic check, the same row cap as a first attempt (P5-F05) — and the proxy
runs it through the same authorization, cost gate, execution and result validation. A
repair is never a shortcut; it is the same pipeline with a different input, and a repair
that cannot re-enter that pipeline is not offered at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from xp_prompt import failures, patterns, sql_candidate, sql_execution
from xp_prompt import sql_guard
from xp_prompt.config import Settings
from xp_prompt.schema_slice import SchemaSlice

logger = logging.getLogger(__name__)

#: Sources a proposal can have.
PATTERN = "pattern"
GENERATED = "generated"
NONE = "none"

#: How much of the failed statement the repair prompt carries.
MAX_FAILED_SQL_CHARS = 900
#: A pattern is offered only if its re-validation produced a statement.
MAX_REPAIR_CORRECTIONS = 1


@dataclass(frozen=True, slots=True)
class RepairAttempt:
    """One repair decision: whether it may happen, from where, and why."""

    allowed: bool
    reason: str
    failure: dict[str, Any]
    source: str = NONE
    sql: str = ""
    dialect: str = ""
    pattern_id: str = ""
    fix_fingerprint: str = ""
    explanation: dict[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "source": self.source,
            "failure": self.failure,
            "sql": self.sql,
            "dialect": self.dialect,
            "pattern_id": self.pattern_id,
            "fix_fingerprint": self.fix_fingerprint,
            "explanation": self.explanation,
        }


def propose(
    settings: Settings,
    *,
    question: str,
    schema: SchemaSlice,
    tenant_key: str,
    stage: str = "execute",
    kind_hint: str = "",
    error_class: str = "",
    sqlstate: str = "",
    errno: str = "",
    message: str = "",
    failed_sql: str = "",
    database: str | None = None,
    dialect: str | None = None,
    max_rows: int = sql_guard.MAX_ROWS,
    attempts: int = 0,
    elapsed_ms: int = 0,
    tokens: int = 0,
    intent: str = "query",
    conversation_id: str | None = None,
) -> RepairAttempt:
    """Decide what, if anything, may be tried next for this failure."""
    event = failures.classify(
        stage=stage,
        kind_hint=kind_hint,
        error_class=error_class,
        sqlstate=sqlstate,
        errno=errno,
        message=message,
    )
    budget = failures.budget_from_settings(settings)
    state = failures.RepairState(
        attempts=max(0, int(attempts)),
        elapsed_ms=max(0, int(elapsed_ms)),
        tokens=max(0, int(tokens)),
    )
    allowed, reason = failures.can_repair(event, state, budget)
    explanation: dict[str, Any] = {
        "budget": budget.to_public_dict(),
        "state": state.to_public_dict(),
    }
    if not allowed:
        logger.info("sql_repair outcome=refused reason=%s kind=%s", reason, event.kind)
        return RepairAttempt(
            allowed=False,
            reason=reason,
            failure=event.to_public_dict(),
            explanation=explanation,
        )

    suppressed: set[str] = set()
    if budget.suppress_failed_fixes:
        suppressed = failures.failed_fixes(
            settings, tenant_key=tenant_key, fingerprint_value=event.fingerprint
        )
    explanation["suppressed_fixes"] = len(suppressed)

    pattern_attempt = _from_pattern(
        settings,
        schema=schema,
        tenant_key=tenant_key,
        event=event,
        database=database,
        dialect=dialect,
        max_rows=max_rows,
        intent=intent,
        suppressed=suppressed,
        explanation=explanation,
    )
    if pattern_attempt is not None:
        _record_attempt(
            settings,
            tenant_key=tenant_key,
            event=event,
            fix=pattern_attempt.fix_fingerprint,
            conversation_id=conversation_id,
            outcome="attempted",
        )
        logger.info(
            "sql_repair outcome=proposed source=pattern kind=%s pattern=%s",
            event.kind,
            pattern_attempt.pattern_id,
        )
        return pattern_attempt

    return _from_generation(
        settings,
        question=question,
        schema=schema,
        tenant_key=tenant_key,
        event=event,
        failed_sql=failed_sql,
        database=database,
        dialect=dialect,
        max_rows=max_rows,
        intent=intent,
        suppressed=suppressed,
        conversation_id=conversation_id,
        explanation=explanation,
    )


def record_outcome(
    settings: Settings,
    *,
    tenant_key: str,
    failure: dict[str, Any],
    succeeded: bool,
    fix_fingerprint_value: str = "",
    pattern_id: str = "",
    sql: str = "",
    schema: SchemaSlice | None = None,
    dialect: str = "",
    conversation_id: str | None = None,
    threshold: int = patterns.DEFAULT_EVIDENCE_THRESHOLD,
) -> dict[str, Any]:
    """Store what happened, and let the knowledge catch up.

    A success on a generated correction creates the pattern that a later failure can
    reuse; a success on a pattern is more evidence for it. A failure is recorded
    against the *fix*, so it is never offered again for this failure (P5-F07).
    """
    fingerprint_value = str((failure or {}).get("fingerprint") or "")
    kind = str((failure or {}).get("kind") or "")
    stage = str((failure or {}).get("stage") or "execute")
    fix = (fix_fingerprint_value or "").strip() or (
        failures.fix_fingerprint(sql) if sql else ""
    )
    recorded = failures.record_attempt(
        settings,
        tenant_key=tenant_key,
        fingerprint_value=fingerprint_value,
        kind=kind,
        stage=stage,
        outcome="succeeded" if succeeded else "failed",
        fix_fingerprint_value=fix,
        conversation_id=conversation_id,
    )
    result: dict[str, Any] = {
        "recorded": recorded,
        "succeeded": succeeded,
        "fix_fingerprint": fix,
        "pattern": None,
        "promoted": False,
    }
    target = pattern_id.strip()
    if succeeded and not target and schema is not None and sql:
        # A correction that worked and is not in the library yet: it becomes a
        # candidate, and only the evidence threshold can promote it (P5-F08).
        target = (
            patterns.record_candidate(
                settings,
                tenant_key=tenant_key,
                failure_fingerprint=fingerprint_value,
                kind=kind,
                stage=stage,
                dialect=dialect,
                schema_fingerprint=schema.fingerprint,
                sql=sql,
                origin="generated",
                tables=None,
            )
            or ""
        )
    if target:
        updated = patterns.record_validation(
            settings,
            tenant_key=tenant_key,
            pattern_id=target,
            succeeded=succeeded,
            evidence=conversation_id or "turn",
            threshold=threshold,
        )
        result["pattern"] = updated
        result["promoted"] = bool(updated and updated.get("promoted"))
    logger.info(
        "sql_repair outcome=recorded succeeded=%s kind=%s pattern=%s promoted=%s",
        succeeded,
        kind,
        bool(target),
        result["promoted"],
    )
    return result


def _from_pattern(
    settings: Settings,
    *,
    schema: SchemaSlice,
    tenant_key: str,
    event: failures.FailureEvent,
    database: str | None,
    dialect: str | None,
    max_rows: int,
    intent: str,
    suppressed: set[str],
    explanation: dict[str, Any],
) -> RepairAttempt | None:
    """The best promoted correction for this exact failure, re-validated.

    A pattern that no longer passes the guard is quarantined rather than skipped
    silently: knowledge that stopped being true must not stay on the books.
    """
    if not dialect:
        try:
            dialect = sql_execution.resolve_dialect(schema, database=database)
        except sql_guard.SqlPolicyError:
            return None
    found = patterns.find(
        settings,
        tenant_key=tenant_key,
        failure_fingerprint=event.fingerprint,
        dialect=dialect,
        schema_fingerprint=schema.fingerprint,
        intent=intent,
        exclude=suppressed,
    )
    explanation["patterns_considered"] = len(found)
    if not found:
        return None
    chosen = found[0]
    explanation["pattern_ranking"] = [
        {
            "id": pattern.id,
            "score": round(pattern.score, 4),
            "factors": {key: round(value, 4) for key, value in pattern.factors.items()},
        }
        for pattern in found[:3]
    ]
    validated = _revalidate(
        chosen.sql or "",
        schema=schema,
        database=database,
        dialect=dialect,
        max_rows=max_rows,
    )
    if validated is None:
        explanation["pattern_rejected"] = "failed_revalidation"
        _quarantine(settings, tenant_key=tenant_key, pattern_id=chosen.id)
        return None
    return RepairAttempt(
        allowed=True,
        reason="allowed",
        failure=event.to_public_dict(),
        source=PATTERN,
        sql=str(validated["sql"]),
        dialect=str(validated.get("dialect") or dialect),
        pattern_id=chosen.id,
        fix_fingerprint=chosen.correction_sha256,
        explanation=explanation,
    )


def _from_generation(
    settings: Settings,
    *,
    question: str,
    schema: SchemaSlice,
    tenant_key: str,
    event: failures.FailureEvent,
    failed_sql: str,
    database: str | None,
    dialect: str | None,
    max_rows: int,
    intent: str,
    suppressed: set[str],
    conversation_id: str | None,
    explanation: dict[str, Any],
) -> RepairAttempt:
    """Ask for one corrected statement, then validate it like a first attempt."""
    note = repair_note(event, failed_sql)
    explanation["correction"] = "generated"
    try:
        candidate = sql_candidate.generate(
            settings,
            question=question,
            schema=schema,
            tenant_key=tenant_key,
            database=database,
            dialect=dialect,
            max_rows=max_rows,
            # The cached statement for this question is the one that just failed.
            bypass_cache=True,
            repair_note=note,
        )
    except sql_candidate.SqlCandidateError as exc:
        explanation["generation_error"] = exc.code
        return RepairAttempt(
            allowed=False,
            reason=f"generation_failed:{exc.code}",
            failure=event.to_public_dict(),
            explanation=explanation,
        )
    except Exception as exc:  # pragma: no cover - provider or transport failure
        explanation["generation_error"] = type(exc).__name__
        return RepairAttempt(
            allowed=False,
            reason="generation_failed",
            failure=event.to_public_dict(),
            explanation=explanation,
        )

    fix = failures.fix_fingerprint(candidate.sql)
    if fix and fix in suppressed:
        # The same shape already failed for this failure: trying it again is the
        # loop P5-F07 exists to stop.
        _record_attempt(
            settings,
            tenant_key=tenant_key,
            event=event,
            fix=fix,
            conversation_id=conversation_id,
            outcome="suppressed",
        )
        explanation["correction"] = "suppressed"
        return RepairAttempt(
            allowed=False,
            reason="duplicate_fix",
            failure=event.to_public_dict(),
            explanation=explanation,
        )

    validated = _revalidate(
        candidate.sql,
        schema=schema,
        database=database,
        dialect=dialect,
        max_rows=max_rows,
    )
    if validated is None:
        explanation["correction"] = "failed_revalidation"
        return RepairAttempt(
            allowed=False,
            reason="invalid_correction",
            failure=event.to_public_dict(),
            explanation=explanation,
        )
    _record_attempt(
        settings,
        tenant_key=tenant_key,
        event=event,
        fix=fix,
        conversation_id=conversation_id,
        outcome="attempted",
    )
    logger.info("sql_repair outcome=proposed source=generated kind=%s", event.kind)
    return RepairAttempt(
        allowed=True,
        reason="allowed",
        failure=event.to_public_dict(),
        source=GENERATED,
        sql=str(validated["sql"]),
        dialect=str(validated.get("dialect") or ""),
        fix_fingerprint=fix,
        explanation=explanation,
    )


def repair_note(event: failures.FailureEvent, failed_sql: str) -> str:
    """What the model is told about the failure — the shape, never the data."""
    lines = [
        "A previous attempt at this question failed.",
        f"Failure kind: {event.kind}.",
        f"What went wrong: {event.hint}",
    ]
    detail = (event.detail or "").strip()
    if detail:
        lines.append(f"The database answered: {detail}")
    statement = " ".join((failed_sql or "").split())[:MAX_FAILED_SQL_CHARS]
    if statement:
        lines.append(f"The statement that failed was:\n{statement}")
    lines.append(
        "Write one corrected statement that avoids that failure. Change only what "
        "caused it; keep the question's meaning."
    )
    return "\n".join(lines)


def _revalidate(
    sql: str, *, schema: SchemaSlice, database: str | None, dialect: str | None, max_rows: int
) -> dict[str, Any] | None:
    """P5-F05: a repair reaches the same guard, semantics and cap as a first attempt."""
    if not (sql or "").strip():
        return None
    try:
        return sql_execution.validate(
            sql, schema=schema, database=database, dialect=dialect, max_rows=max_rows
        )
    except sql_guard.SqlPolicyError as exc:
        logger.info("sql_repair outcome=blocked code=%s", exc.code)
        return None


def _record_attempt(
    settings: Settings,
    *,
    tenant_key: str,
    event: failures.FailureEvent,
    fix: str,
    conversation_id: str | None,
    outcome: str,
) -> None:
    failures.record_attempt(
        settings,
        tenant_key=tenant_key,
        fingerprint_value=event.fingerprint,
        kind=event.kind,
        stage=event.stage,
        outcome=outcome,
        fix_fingerprint_value=fix,
        conversation_id=conversation_id,
    )


def _quarantine(settings: Settings, *, tenant_key: str, pattern_id: str) -> None:
    try:
        patterns.set_state(
            settings,
            tenant_key=tenant_key,
            pattern_id=pattern_id,
            action="quarantine",
            reason="failed_revalidation",
        )
    except patterns.PatternError:  # pragma: no cover - already disabled
        logger.info("sql_repair outcome=quarantine_skipped")
