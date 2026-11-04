"""Cost guard for a statement that is about to run (P4-F08).

The AI service never talks to an analytics database, so it cannot run ``EXPLAIN``
itself — Superset does, and hands back a *normalized estimate*. This module is the
policy half: given an estimate and a budget, decide whether the statement may run,
and say why in a way a user can act on.

Three properties matter more than the numbers:

* **a missing estimate is not a free pass.** A deployment can require a plan; when
  it does not, the answer still carries a warning, so "we did not measure this" is
  visible instead of implied.
* **the budget is owned here**, not by the caller that measures — a proxy cannot
  raise its own limit by sending a friendlier estimate.
* **refusals are stable codes**, so the workspace can offer "refine the question"
  rather than showing a database error (and the audit trail stays content-free).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Plan sources a caller may report. ``unavailable`` is honest, not an error: some
#: dialects cannot explain a statement without running it.
PLAN_SOURCES: frozenset[str] = frozenset({"explain", "dry_run", "unavailable"})

#: Refusal codes. ``ok`` is the only allowed verdict that carries no refusal.
OK = "ok"
ESTIMATED_ROWS_TOO_HIGH = "estimated_rows_too_high"
ESTIMATED_COST_TOO_HIGH = "estimated_cost_too_high"
PLAN_TOO_COMPLEX = "plan_too_complex"
PLAN_UNAVAILABLE = "plan_unavailable"
RUNTIME_BUDGET_EXCEEDED = "runtime_budget_exceeded"


@dataclass(frozen=True, slots=True)
class Budget:
    """The limits one statement is held to. Owned by the service."""

    max_rows: int = 5_000_000
    max_cost: float = 1_000_000.0
    max_plan_nodes: int = 400
    max_runtime_ms: int = 20_000
    require_plan: bool = False

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "max_rows": self.max_rows,
            "max_cost": self.max_cost,
            "max_plan_nodes": self.max_plan_nodes,
            "max_runtime_ms": self.max_runtime_ms,
            "require_plan": self.require_plan,
        }


@dataclass(frozen=True, slots=True)
class Estimate:
    """A normalized cost estimate: whatever the engine could tell us, bounded."""

    source: str = "unavailable"
    rows: int | None = None
    cost: float | None = None
    plan_nodes: int = 0

    @classmethod
    def from_payload(cls, raw: Any) -> "Estimate":
        """Never trust the caller's shape: coerce, clamp, or degrade to unavailable."""
        if not isinstance(raw, dict):
            return cls()
        source = str(raw.get("source") or "").strip().lower()
        if source not in PLAN_SOURCES:
            source = "unavailable"
        rows = _non_negative_int(raw.get("rows"))
        cost = _non_negative_float(raw.get("cost"))
        nodes = _non_negative_int(raw.get("plan_nodes")) or 0
        if source == "unavailable":
            # An "unavailable" estimate that still carries numbers is a caller
            # mistake; keeping the numbers would let a proxy dodge a refusal.
            return cls(source="unavailable")
        return cls(source=source, rows=rows, cost=cost, plan_nodes=nodes)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "rows": self.rows,
            "cost": self.cost,
            "plan_nodes": self.plan_nodes,
        }


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether the statement may run, and what to tell the user."""

    allowed: bool
    code: str
    message: str
    estimate: Estimate
    budget: Budget

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "message": self.message,
            "estimate": self.estimate.to_public_dict(),
            "budget": self.budget.to_public_dict(),
        }

    def warning_items(self) -> list[str]:
        """Warnings worth showing even when the statement is allowed."""
        if self.allowed and self.code == PLAN_UNAVAILABLE:
            return ["No cost estimate was available for this query."]
        if self.allowed and self.estimate.rows:
            return [
                f"Estimated {self.estimate.rows:,} rows before the "
                f"{self.estimate.source} was run."
            ]
        return []


def evaluate(estimate: Estimate | None, budget: Budget) -> Verdict:
    """Apply the budget to an estimate. Pure, so it is cheap to test exhaustively."""
    current = estimate or Estimate()
    if current.source == "unavailable":
        if budget.require_plan:
            return Verdict(
                allowed=False,
                code=PLAN_UNAVAILABLE,
                message=(
                    "This workspace requires a cost estimate before a query runs, and "
                    "one could not be produced. Try a simpler question."
                ),
                estimate=current,
                budget=budget,
            )
        return Verdict(
            allowed=True,
            code=PLAN_UNAVAILABLE,
            message="No cost estimate was available; the query was allowed anyway.",
            estimate=current,
            budget=budget,
        )

    if current.rows is not None and current.rows > budget.max_rows:
        return Verdict(
            allowed=False,
            code=ESTIMATED_ROWS_TOO_HIGH,
            message=(
                f"This query would scan about {current.rows:,} rows, above this "
                f"workspace's limit of {budget.max_rows:,}. Narrow the question — "
                "a shorter time range, a single course, or fewer columns."
            ),
            estimate=current,
            budget=budget,
        )

    if current.cost is not None and current.cost > budget.max_cost:
        return Verdict(
            allowed=False,
            code=ESTIMATED_COST_TOO_HIGH,
            message=(
                f"This query is estimated at {current.cost:,.0f} cost units, above "
                f"the limit of {budget.max_cost:,.0f}. Narrow the question and try "
                "again."
            ),
            estimate=current,
            budget=budget,
        )

    if current.plan_nodes > budget.max_plan_nodes:
        return Verdict(
            allowed=False,
            code=PLAN_TOO_COMPLEX,
            message=(
                "This query plan is too complex for the workspace budget. Ask for "
                "one metric at a time."
            ),
            estimate=current,
            budget=budget,
        )

    return Verdict(
        allowed=True,
        code=OK,
        message="Within budget.",
        estimate=current,
        budget=budget,
    )


def runtime_exceeded(duration_ms: int | float | None, budget: Budget) -> bool:
    """Post-execution check: the answer took longer than the budget allows.

    A wall-clock limit cannot be enforced from here (the service has no database
    connection, and a synchronous driver cannot be interrupted reliably). The
    statement is bounded by the row cap and the read-only role; this check keeps
    the *budget* honest by refusing to present a slow answer as a cheap one.
    """
    try:
        elapsed = int(duration_ms or 0)
    except (TypeError, ValueError):
        return False
    return elapsed > budget.max_runtime_ms


def _non_negative_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _non_negative_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None
