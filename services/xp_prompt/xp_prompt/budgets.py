"""Budgets that are checked before the money is spent, not after (P9-F09).

A budget enforced after the fact is a report, not a limit. The difference is a
reservation: a call *claims* headroom atomically before it starts, and settles the
claim against what it actually cost when it finishes.

The reservation is one conditional UPDATE. Two requests that would together
exceed a hard limit cannot both see headroom, because the row is only updated when
``spent + reserved + this claim`` still fits. That is what makes the tolerance one
in-flight claim rather than however many workers happen to be busy.

The rules:

* **a claim is nominal, the settlement is real.** The claim is a fixed nominal
  amount, because the cost of a call is not knowable until the provider answers.
  It is released in full at settlement and replaced by the actual cost, so a busy
  period over-reserves temporarily and never pays twice.
* **``hard_stop`` refuses, ``warn`` only reports.** A budget may be advisory; the
  threshold is what turns it into a refusal, so "80% warning" and "do not spend
  more" are different rows, not one row with a mood.
* **an unanswered call still settles.** A failed attempt settles at zero cost but
  still releases its claim, so a provider outage cannot permanently consume
  headroom.
* **scope is explicit.** ``global``, ``module`` and ``user`` rows are all evaluated
  in one transaction, and the strictest refusal wins.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from xp_prompt.config import Settings
from xp_prompt.db import connect

logger = logging.getLogger(__name__)

GLOBAL = "global"
USER = "user"
MODULE = "module"
SCOPES: tuple[str, ...] = (GLOBAL, USER, MODULE)

DAY = "day"
MONTH = "month"
PERIODS: tuple[str, ...] = (DAY, MONTH)

OK = "ok"
WARN = "warn"
BLOCKED = "blocked"

#: What a single call claims before it runs. Deliberately small: the point is to
#: serialise the boundary, not to predict the invoice.
DEFAULT_CLAIM_MICROS = 10_000
DEFAULT_WARN_PERCENT = 80
MAX_WARN_PERCENT = 100
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 200


class BudgetError(ValueError):
    """A refused budget operation; ``code`` is stable and safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BudgetBlocked(RuntimeError):
    """A hard limit has no headroom left; the call must not be made."""

    def __init__(self, budget_id: str, reason: str) -> None:
        super().__init__(reason)
        self.budget_id = budget_id
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Budget:
    id: str
    scope: str
    scope_ref: str
    period: str
    cost_micros_limit: int | None
    tokens_limit: int | None
    warn_percent: int
    hard_stop: bool
    enabled: bool
    currency: str = ""

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope,
            "scope_ref": self.scope_ref,
            "period": self.period,
            "cost_micros_limit": self.cost_micros_limit,
            "tokens_limit": self.tokens_limit,
            "warn_percent": self.warn_percent,
            "hard_stop": self.hard_stop,
            "enabled": self.enabled,
            "currency": self.currency,
        }


@dataclass(frozen=True, slots=True)
class Claim:
    """One reserved row, and what it reserved, so settlement can undo it exactly."""

    budget_id: str
    window_id: str
    reserved_micros: int
    reserved_tokens: int


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of one pre-flight check, strictest budget first."""

    outcome: str
    reason: str = ""
    budget_id: str = ""
    claims: tuple[Claim, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.outcome != BLOCKED

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "budget_id": self.budget_id,
            "claimed": len(self.claims),
        }


def _default_warn_percent(settings: Settings, tenant_key: str) -> int:
    """The deployment's warning line for a new budget, bounded like every other."""
    from xp_prompt import settings_registry

    values = settings_registry.live(settings, tenant_key=tenant_key)
    resolved = values.get("usage.default_budget_warn_percent")
    percent = int(resolved.value) if resolved is not None else DEFAULT_WARN_PERCENT
    return max(1, min(percent, MAX_WARN_PERCENT))


def define(
    settings: Settings,
    *,
    tenant_key: str,
    scope: str,
    period: str,
    scope_ref: str = "",
    cost_micros_limit: int | None = None,
    tokens_limit: int | None = None,
    warn_percent: int | None = None,
    hard_stop: bool = True,
    enabled: bool = True,
    currency: str = "",
) -> Budget:
    """Create or update one budget definition.

    ``warn_percent`` defaults to the deployment's ``usage.default_budget_warn_percent``
    rather than a constant, so a new budget starts at the line the operator chose
    instead of the one baked in at build time (P10-F07).
    """
    if warn_percent is None:
        warn_percent = _default_warn_percent(settings, tenant_key)
    clean_scope = (scope or "").strip().lower()
    clean_period = (period or "").strip().lower()
    if clean_scope not in SCOPES:
        raise BudgetError("invalid_scope", f"scope must be one of {', '.join(SCOPES)}")
    if clean_period not in PERIODS:
        raise BudgetError(
            "invalid_period", f"period must be one of {', '.join(PERIODS)}"
        )
    if clean_scope == GLOBAL:
        reference = ""
    else:
        reference = " ".join(str(scope_ref or "").split())[:64]
        if not reference:
            raise BudgetError(
                "scope_ref_required", f"a {clean_scope} budget needs a reference"
            )
    if cost_micros_limit is None and tokens_limit is None:
        raise BudgetError(
            "limit_required", "a budget needs a cost limit or a token limit"
        )
    for label, value in (
        ("cost_micros_limit", cost_micros_limit),
        ("tokens_limit", tokens_limit),
    ):
        if value is not None and int(value) <= 0:
            raise BudgetError("invalid_limit", f"{label} must be positive")
    if not 0 < int(warn_percent) <= MAX_WARN_PERCENT:
        raise BudgetError(
            "invalid_warn_percent", f"warn_percent must be 1..{MAX_WARN_PERCENT}"
        )

    with connect(settings) as connection, connection.cursor() as cursor:
        tenant_id = _tenant_id(cursor, tenant_key)
        cursor.execute(
            """
            INSERT INTO ai_usage_budget (
                tenant_id, scope, scope_ref, period, cost_micros_limit,
                tokens_limit, warn_percent, hard_stop, enabled, currency
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, scope, scope_ref, period) DO UPDATE
            SET cost_micros_limit = EXCLUDED.cost_micros_limit,
                tokens_limit = EXCLUDED.tokens_limit,
                warn_percent = EXCLUDED.warn_percent,
                hard_stop = EXCLUDED.hard_stop,
                enabled = EXCLUDED.enabled,
                currency = EXCLUDED.currency,
                updated_at = CURRENT_TIMESTAMP
            RETURNING id, scope, scope_ref, period, cost_micros_limit,
                      tokens_limit, warn_percent, hard_stop, enabled, currency
            """,
            (
                tenant_id,
                clean_scope,
                reference,
                clean_period,
                _int_or_none(cost_micros_limit),
                _int_or_none(tokens_limit),
                int(warn_percent),
                bool(hard_stop),
                bool(enabled),
                (currency or "").strip().upper()[:3],
            ),
        )
        row = cursor.fetchone()
    logger.info(
        "budget_defined scope=%s ref=%s period=%s hard_stop=%s",
        clean_scope,
        reference or "-",
        clean_period,
        hard_stop,
    )
    return _budget(row)


def list_budgets(
    settings: Settings, *, tenant_key: str, limit: int = DEFAULT_LIST_LIMIT
) -> list[Budget]:
    bounded = max(1, min(int(limit), MAX_LIST_LIMIT))
    with connect(settings) as connection, connection.cursor() as cursor:
        tenant_id = _tenant_id(cursor, tenant_key)
        cursor.execute(
            """
            SELECT id, scope, scope_ref, period, cost_micros_limit, tokens_limit,
                   warn_percent, hard_stop, enabled, currency
            FROM ai_usage_budget
            WHERE tenant_id = %s
            ORDER BY scope, scope_ref, period
            LIMIT %s
            """,
            (tenant_id, bounded),
        )
        rows = cursor.fetchall()
    return [_budget(row) for row in rows]


def delete_budget(settings: Settings, budget_id: str, *, tenant_key: str) -> bool:
    with connect(settings) as connection, connection.cursor() as cursor:
        tenant_id = _tenant_id(cursor, tenant_key)
        cursor.execute(
            """
            DELETE FROM ai_usage_budget
            WHERE id = %s AND tenant_id = %s
            RETURNING id
            """,
            (str(budget_id), tenant_id),
        )
        return cursor.fetchone() is not None


def reserve(
    settings: Settings,
    *,
    tenant_key: str,
    module: str = "",
    superset_user_id: int | None = None,
    estimate_micros: int = DEFAULT_CLAIM_MICROS,
    estimate_tokens: int = 0,
) -> Decision:
    """Claim headroom for one call, atomically, across every applicable budget.

    Every budget is checked in a single transaction. The first one without room
    rolls the whole claim back, so a call is never half-reserved — either it has
    headroom everywhere it must, or it made no claim at all.
    """
    claim_micros = max(0, int(estimate_micros))
    claim_tokens = max(0, int(estimate_tokens))
    try:
        with connect(settings) as connection, connection.cursor() as cursor:
            tenant_id = _tenant_id(cursor, tenant_key)
            budgets = _applicable(cursor, tenant_id, module, superset_user_id)
            if not budgets:
                return Decision(OK, "no budget defined")

            claims: list[Claim] = []
            worst = OK
            reason = ""
            for budget in budgets:
                window_id, spent, reserved = _window(cursor, budget)
                total_spent = spent + reserved
                limit = budget.cost_micros_limit
                if limit is not None and total_spent + claim_micros > limit:
                    if budget.hard_stop:
                        raise BudgetBlocked(
                            budget.id,
                            _limit_message(budget, total_spent, limit),
                        )
                    worst = WARN
                    reason = reason or _limit_message(budget, total_spent, limit)
                    continue
                if limit is not None and budget.warn_percent:
                    threshold = limit * int(budget.warn_percent) // 100
                    if total_spent + claim_micros >= threshold:
                        worst = WARN
                        reason = reason or _limit_message(budget, total_spent, limit)
                claimed = _claim(
                    cursor, budget, window_id, claim_micros, claim_tokens
                )
                if claimed is None:  # pragma: no cover - only under a race
                    raise BudgetBlocked(budget.id, "no headroom left")
                claims.append(
                    Claim(
                        budget_id=budget.id,
                        window_id=window_id,
                        reserved_micros=claim_micros,
                        reserved_tokens=claim_tokens,
                    )
                )
            return Decision(worst, reason, claims=tuple(claims))
    except BudgetBlocked as blocked:
        _count_block(settings, blocked.budget_id)
        logger.warning("budget_blocked budget=%s reason=%s", blocked.budget_id,
                       blocked.reason)
        return Decision(BLOCKED, blocked.reason, blocked.budget_id)


def settle(
    settings: Settings,
    decision: Decision,
    *,
    actual_micros: int | None = None,
    actual_tokens: int | None = None,
) -> None:
    """Release every claim and charge what the call actually cost.

    A call that never reached the provider settles at zero, which is why a
    failure must still call this: the claim is the only thing standing between a
    broken provider and a budget that is permanently full.
    """
    if not decision.claims:
        return
    spent_micros = max(0, int(actual_micros or 0))
    spent_tokens = max(0, int(actual_tokens or 0))
    try:
        with connect(settings) as connection, connection.cursor() as cursor:
            for claim in decision.claims:
                cursor.execute(
                    """
                    UPDATE ai_usage_budget_window
                    SET reserved_micros = GREATEST(0, reserved_micros - %s),
                        reserved_tokens = GREATEST(0, reserved_tokens - %s),
                        spent_micros = spent_micros + %s,
                        spent_tokens = spent_tokens + %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (
                        claim.reserved_micros,
                        claim.reserved_tokens,
                        spent_micros,
                        spent_tokens,
                        claim.window_id,
                    ),
                )
    except Exception:  # pragma: no cover - settlement is best effort
        logger.warning("budget_settle_failed claims=%d", len(decision.claims),
                       exc_info=True)


def status(settings: Settings, *, tenant_key: str) -> dict[str, Any]:
    """Every budget with its current window, for the admin cards."""
    with connect(settings) as connection, connection.cursor() as cursor:
        tenant_id = _tenant_id(cursor, tenant_key)
        cursor.execute(
            """
            SELECT b.id, b.scope, b.scope_ref, b.period, b.cost_micros_limit,
                   b.tokens_limit, b.warn_percent, b.hard_stop, b.enabled,
                   b.currency,
                   COALESCE(w.spent_micros, 0) AS spent_micros,
                   COALESCE(w.reserved_micros, 0) AS reserved_micros,
                   COALESCE(w.spent_tokens, 0) AS spent_tokens,
                   COALESCE(w.blocked_count, 0) AS blocked_count,
                   w.period_start
            FROM ai_usage_budget b
            LEFT JOIN ai_usage_budget_window w
              ON w.budget_id = b.id
             AND w.period_start = date_trunc(
                    CASE b.period WHEN 'day' THEN 'day' ELSE 'month' END,
                    CURRENT_TIMESTAMP
                 )
            WHERE b.tenant_id = %s
            ORDER BY b.scope, b.scope_ref, b.period
            """,
            (tenant_id,),
        )
        rows = cursor.fetchall()

    budgets: list[dict[str, Any]] = []
    for row in rows:
        limit = row.get("cost_micros_limit")
        spent = int(row.get("spent_micros") or 0)
        reserved = int(row.get("reserved_micros") or 0)
        percent = (
            min(100, int((spent + reserved) * 100 // int(limit)))
            if limit
            else 0
        )
        budgets.append(
            {
                **_budget(row).to_public_dict(),
                "spent_micros": spent,
                "reserved_micros": reserved,
                "spent_tokens": int(row.get("spent_tokens") or 0),
                "blocked_count": int(row.get("blocked_count") or 0),
                "percent_used": percent,
                "state": _state(row, spent + reserved, percent),
                "period_start": _isoformat(row.get("period_start")),
            }
        )
    return {"tenant": tenant_key, "count": len(budgets), "budgets": budgets}


class Guard:
    """A per-request reservation the router brackets a call with."""

    def __init__(
        self, settings: Settings, context: Any, *, announce: Any = None
    ) -> None:
        self._settings = settings
        self._context = context
        self._announce = announce
        self._decision: Decision | None = None

    def open(self) -> None:
        """Reserve before the first attempt; raise when a hard limit refuses.

        A failure in the guard itself fails open. Refusing every AI call because
        the budget bookkeeping is broken would be a worse outcome than briefly
        running without a limit, and the error is logged either way.
        """
        module = getattr(self._context, "module", "") or ""
        user_id = getattr(self._context, "superset_user_id", None)
        tenant_key = getattr(self._context, "tenant_key", "default")
        try:
            decision = reserve(
                self._settings,
                tenant_key=tenant_key,
                module=module,
                superset_user_id=user_id,
            )
        except Exception:  # pragma: no cover - guard must not break AI
            logger.warning("budget_guard_failed_open", exc_info=True)
            return
        if not decision.allowed:
            raise BudgetError("budget_exhausted", decision.reason)
        self._decision = decision
        if decision.outcome == WARN and self._announce is not None:
            self._announce(decision)

    def close(self, attempts: Iterable[Any] = ()) -> None:
        """Settle the claim against what the attempts actually cost."""
        if self._decision is None:
            return
        decision = self._decision
        self._decision = None
        try:
            from xp_prompt import usage

            micros, tokens = usage.cost_of(self._settings, attempts)
        except Exception:  # pragma: no cover - settlement is best effort
            logger.warning("budget_cost_lookup_failed", exc_info=True)
            micros, tokens = 0, 0
        settle(
            self._settings, decision, actual_micros=micros, actual_tokens=tokens
        )


def guard(
    settings: Settings, context: Any, *, announce: Any = None
) -> Guard:
    """Build the guard a router brackets its call with (P9-F09)."""
    return Guard(settings, context, announce=announce)


def _applicable(
    cursor: Any, tenant_id: str, module: str, superset_user_id: int | None
) -> list[Budget]:
    """Every enabled budget that covers this call, strictest scope first."""
    cursor.execute(
        """
        SELECT id, scope, scope_ref, period, cost_micros_limit, tokens_limit,
               warn_percent, hard_stop, enabled, currency
        FROM ai_usage_budget
        WHERE tenant_id = %s
          AND enabled = true
          AND (
              scope = 'global'
              OR (scope = 'module' AND scope_ref = %s)
              OR (scope = 'user' AND scope_ref = %s)
          )
        ORDER BY CASE scope WHEN 'global' THEN 1 WHEN 'module' THEN 2 ELSE 3 END,
                 period
        """,
        (tenant_id, module or "", str(superset_user_id or "")),
    )
    return [_budget(row) for row in cursor.fetchall()]


def _window(cursor: Any, budget: Budget) -> tuple[str, int, int]:
    """Get or create this period's window; returns its id and current counters.

    The window start is computed by the database, so every process agrees on where
    a day or a month begins.
    """
    cursor.execute(
        """
        INSERT INTO ai_usage_budget_window (budget_id, period_start)
        VALUES (
            %s,
            date_trunc(CASE %s WHEN 'day' THEN 'day' ELSE 'month' END,
                       CURRENT_TIMESTAMP)
        )
        ON CONFLICT (budget_id, period_start) DO UPDATE
        SET updated_at = CURRENT_TIMESTAMP
        RETURNING id, spent_micros, reserved_micros
        """,
        (budget.id, budget.period),
    )
    row = cursor.fetchone()
    return str(row["id"]), int(row["spent_micros"]), int(row["reserved_micros"])


def _claim(
    cursor: Any,
    budget: Budget,
    window_id: str,
    micros: int,
    tokens: int,
) -> str | None:
    """Take the claim only if it still fits — the atomic part of the guard."""
    limit = budget.cost_micros_limit
    token_limit = budget.tokens_limit
    cursor.execute(
        """
        UPDATE ai_usage_budget_window
        SET reserved_micros = reserved_micros + %s,
            reserved_tokens = reserved_tokens + %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
          AND (%s::bigint IS NULL
               OR spent_micros + reserved_micros + %s::bigint <= %s::bigint)
          AND (%s::bigint IS NULL
               OR spent_tokens + reserved_tokens + %s::bigint <= %s::bigint)
        RETURNING id
        """,
        (
            micros,
            tokens,
            window_id,
            limit,
            micros,
            limit,
            token_limit,
            tokens,
            token_limit,
        ),
    )
    row = cursor.fetchone()
    return str(row["id"]) if row else None


def _count_block(settings: Settings, budget_id: str) -> None:
    """Record the refusal, so the admin sees demand that was turned away."""
    if not budget_id:
        return
    try:
        with connect(settings) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO ai_usage_budget_window (budget_id, period_start)
                SELECT %s, date_trunc(
                    CASE b.period WHEN 'day' THEN 'day' ELSE 'month' END,
                    CURRENT_TIMESTAMP
                )
                FROM ai_usage_budget b
                WHERE b.id = %s
                ON CONFLICT (budget_id, period_start) DO UPDATE
                SET blocked_count = ai_usage_budget_window.blocked_count + 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (str(budget_id), str(budget_id)),
            )
    except Exception:  # pragma: no cover - bookkeeping only
        logger.warning("budget_block_count_failed budget=%s", budget_id)


def _limit_message(budget: Budget, spent: int, limit: int) -> str:
    where = f" for {budget.scope} {budget.scope_ref}" if budget.scope_ref else ""
    return (
        f"the {budget.period} AI budget{where} is used up "
        f"({spent} of {limit} micros)"
    )


def _state(row: Mapping[str, Any], used: int, percent: int) -> str:
    if not row.get("enabled"):
        return "disabled"
    limit = row.get("cost_micros_limit")
    if limit and used >= int(limit):
        return "exhausted" if row.get("hard_stop") else "over"
    if percent >= int(row.get("warn_percent") or DEFAULT_WARN_PERCENT):
        return "warning"
    return "ok"


def _budget(row: Mapping[str, Any]) -> Budget:
    return Budget(
        id=str(row["id"]),
        scope=row["scope"],
        scope_ref=row["scope_ref"] or "",
        period=row["period"],
        cost_micros_limit=_int_or_none(row.get("cost_micros_limit")),
        tokens_limit=_int_or_none(row.get("tokens_limit")),
        warn_percent=int(row.get("warn_percent") or DEFAULT_WARN_PERCENT),
        hard_stop=bool(row.get("hard_stop")),
        enabled=bool(row.get("enabled")),
        currency=row.get("currency") or "",
    )


def _tenant_id(cursor: Any, tenant_key: str) -> str:
    cursor.execute("SELECT id FROM ai_tenant WHERE key = %s", (tenant_key,))
    row = cursor.fetchone()
    if row is None:
        raise BudgetError("unknown_tenant", "tenant is not provisioned")
    return str(row["id"])


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def new_id() -> str:
    return str(uuid.uuid4())
