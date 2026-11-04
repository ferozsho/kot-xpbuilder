"""Tests for the cost guard (P4-F08)."""

from __future__ import annotations

import unittest

from xp_prompt import cost_guard


def estimate(**overrides):
    payload = {"source": "explain", "rows": 100, "cost": 12.5, "plan_nodes": 3}
    payload.update(overrides)
    return cost_guard.Estimate.from_payload(payload)


class BudgetTest(unittest.TestCase):
    def test_defaults_are_reported_publicly(self) -> None:
        budget = cost_guard.Budget()

        self.assertEqual(
            set(budget.to_public_dict()),
            {
                "max_rows",
                "max_cost",
                "max_plan_nodes",
                "max_runtime_ms",
                "require_plan",
            },
        )


class EstimateParsingTest(unittest.TestCase):
    def test_a_good_estimate_is_kept(self) -> None:
        parsed = estimate()

        self.assertEqual(parsed.source, "explain")
        self.assertEqual(parsed.rows, 100)
        self.assertAlmostEqual(parsed.cost or 0, 12.5)
        self.assertEqual(parsed.plan_nodes, 3)

    def test_an_unknown_source_degrades_to_unavailable(self) -> None:
        parsed = estimate(source="guess")

        self.assertEqual(parsed.source, "unavailable")
        self.assertIsNone(parsed.rows)

    def test_unavailable_never_carries_numbers(self) -> None:
        """A caller must not be able to say "unavailable" and also report numbers."""
        parsed = estimate(source="unavailable", rows=1, cost=1.0, plan_nodes=1)

        self.assertIsNone(parsed.rows)
        self.assertIsNone(parsed.cost)
        self.assertEqual(parsed.plan_nodes, 0)

    def test_negative_and_garbage_values_are_dropped(self) -> None:
        parsed = estimate(rows=-5, cost="not a number", plan_nodes=-1)

        self.assertIsNone(parsed.rows)
        self.assertIsNone(parsed.cost)
        self.assertEqual(parsed.plan_nodes, 0)

    def test_a_non_object_payload_is_unavailable(self) -> None:
        self.assertEqual(
            cost_guard.Estimate.from_payload("nope").source, "unavailable"
        )


class VerdictTest(unittest.TestCase):
    def test_a_cheap_statement_is_allowed(self) -> None:
        verdict = cost_guard.evaluate(estimate(), cost_guard.Budget())

        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.code, cost_guard.OK)

    def test_too_many_estimated_rows_is_refused(self) -> None:
        verdict = cost_guard.evaluate(
            estimate(rows=50_000_000), cost_guard.Budget(max_rows=1_000)
        )

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.code, cost_guard.ESTIMATED_ROWS_TOO_HIGH)
        self.assertIn("Narrow the question", verdict.message)

    def test_too_much_estimated_cost_is_refused(self) -> None:
        verdict = cost_guard.evaluate(
            estimate(cost=9_000_000.0), cost_guard.Budget(max_cost=1_000.0)
        )

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.code, cost_guard.ESTIMATED_COST_TOO_HIGH)

    def test_a_very_large_plan_is_refused(self) -> None:
        verdict = cost_guard.evaluate(
            estimate(plan_nodes=500), cost_guard.Budget(max_plan_nodes=100)
        )

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.code, cost_guard.PLAN_TOO_COMPLEX)

    def test_unavailable_is_allowed_by_default_but_flagged(self) -> None:
        verdict = cost_guard.evaluate(
            cost_guard.Estimate(source="unavailable"), cost_guard.Budget()
        )

        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.code, cost_guard.PLAN_UNAVAILABLE)
        self.assertEqual(
            verdict.warning_items(), ["No cost estimate was available for this query."]
        )

    def test_unavailable_is_refused_when_a_plan_is_required(self) -> None:
        verdict = cost_guard.evaluate(
            cost_guard.Estimate(source="unavailable"), cost_guard.Budget(require_plan=True)
        )

        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.code, cost_guard.PLAN_UNAVAILABLE)

    def test_an_allowed_estimate_reports_what_it_saw(self) -> None:
        verdict = cost_guard.evaluate(estimate(rows=1_234), cost_guard.Budget())

        self.assertEqual(
            verdict.warning_items(),
            ["Estimated 1,234 rows before the explain was run."],
        )

    def test_rows_are_checked_before_cost(self) -> None:
        """The actionable message wins: row count is what a user can reduce."""
        verdict = cost_guard.evaluate(
            estimate(rows=1_000_000, cost=5_000_000.0),
            cost_guard.Budget(max_rows=10, max_cost=10.0),
        )

        self.assertEqual(verdict.code, cost_guard.ESTIMATED_ROWS_TOO_HIGH)

    def test_the_verdict_payload_is_content_free(self) -> None:
        payload = cost_guard.evaluate(estimate(), cost_guard.Budget()).to_public_dict()

        self.assertEqual(
            sorted(payload), ["allowed", "budget", "code", "estimate", "message"]
        )
        self.assertNotIn("sql", payload)


class RuntimeBudgetTest(unittest.TestCase):
    def test_a_fast_answer_is_within_budget(self) -> None:
        self.assertFalse(
            cost_guard.runtime_exceeded(500, cost_guard.Budget(max_runtime_ms=1_000))
        )

    def test_a_slow_answer_exceeds_the_budget(self) -> None:
        self.assertTrue(
            cost_guard.runtime_exceeded(5_000, cost_guard.Budget(max_runtime_ms=1_000))
        )

    def test_garbage_runtime_does_not_refuse(self) -> None:
        self.assertFalse(cost_guard.runtime_exceeded(None, cost_guard.Budget()))
        self.assertFalse(
            cost_guard.runtime_exceeded("slow", cost_guard.Budget())  # type: ignore[arg-type]
        )


if __name__ == "__main__":
    unittest.main()
