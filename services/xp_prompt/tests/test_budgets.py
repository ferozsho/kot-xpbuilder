"""Tests for AI budgets: scope rules, the atomic claim, and honest settlement.

The claim is the whole point of the feature, so the tests concentrate on it: a
budget that is advisory must never refuse, a hard budget must refuse without
leaving a half-claim behind, and a call that fails must give its headroom back.
"""

from __future__ import annotations

import unittest
from typing import Self
from unittest import mock

from xp_prompt import budgets, usage
from xp_prompt.config import Settings
from xp_prompt.providers.types import AttemptSummary, TokenUsage

ROOT_SECRET = "unit-test-root-secret-value"
TENANT_ID = "11111111-1111-1111-1111-111111111111"
BUDGET_ID = "22222222-2222-2222-2222-222222222222"
WINDOW_ID = "33333333-3333-3333-3333-333333333333"


def settings() -> Settings:
    return Settings.from_env(
        {
            "XP_PROMPT_POSTGRES_HOST": "127.0.0.1",
            "XP_PROMPT_POSTGRES_PORT": "5432",
            "XP_PROMPT_POSTGRES_USER": "xprompt",
            "XP_PROMPT_POSTGRES_PASSWORD": "database-secret",
            "XP_PROMPT_POSTGRES_DB": "xprompt",
            "XP_PROMPT_REDIS_HOST": "127.0.0.1",
            "XP_PROMPT_REDIS_PORT": "6379",
            "XP_PROMPT_REDIS_PASSWORD": "redis-secret",
            "XP_PROMPT_REDIS_DB": "2",
            "XP_PROMPT_QDRANT_URL": "http://xp-prompt-qdrant:6333",
            "XP_PROMPT_SERVICE_SECRET": ROOT_SECRET,
        }
    )


class _FakeCursor:
    """Scripted rows per statement; records SQL so the claim can be inspected."""

    def __init__(self, rows: list[dict | None] | None = None) -> None:
        self._rows = list(rows or [])
        self.statements: list[str] = []
        self.params: list[object] = []
        self._current: dict | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append(sql)
        self.params.append(params)
        self._current = self._rows.pop(0) if self._rows else None

    def fetchone(self) -> dict | None:
        return self._current

    def fetchall(self) -> list:
        return [self._current] if self._current else []


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return self._cursor


def budget_row(**overrides) -> dict:
    row = {
        "id": BUDGET_ID,
        "scope": "global",
        "scope_ref": "",
        "period": "day",
        "cost_micros_limit": 1_000_000,
        "tokens_limit": None,
        "warn_percent": 80,
        "hard_stop": True,
        "enabled": True,
        "currency": "usd",
    }
    row.update(overrides)
    return row


def window_row(spent: int = 0, reserved: int = 0) -> dict:
    return {
        "id": WINDOW_ID,
        "spent_micros": spent,
        "reserved_micros": reserved,
    }


class DefinitionTests(unittest.TestCase):
    """A budget that cannot mean anything is refused at definition time."""

    def define(self, **overrides):
        fields = {
            "tenant_key": "default",
            "scope": "global",
            "period": "day",
            "cost_micros_limit": 1_000_000,
        }
        fields.update(overrides)
        return budgets.define(settings(), **fields)

    def test_an_unknown_scope_is_refused(self) -> None:
        with self.assertRaises(budgets.BudgetError) as caught:
            self.define(scope="team")
        self.assertEqual(caught.exception.code, "invalid_scope")

    def test_an_unknown_period_is_refused(self) -> None:
        with self.assertRaises(budgets.BudgetError) as caught:
            self.define(period="week")
        self.assertEqual(caught.exception.code, "invalid_period")

    def test_a_scoped_budget_needs_its_reference(self) -> None:
        with self.assertRaises(budgets.BudgetError) as caught:
            self.define(scope="module", scope_ref="  ")
        self.assertEqual(caught.exception.code, "scope_ref_required")

    def test_a_budget_needs_at_least_one_limit(self) -> None:
        with self.assertRaises(budgets.BudgetError) as caught:
            self.define(cost_micros_limit=None)
        self.assertEqual(caught.exception.code, "limit_required")

    def test_a_negative_limit_is_refused(self) -> None:
        with self.assertRaises(budgets.BudgetError) as caught:
            self.define(cost_micros_limit=-5)
        self.assertEqual(caught.exception.code, "invalid_limit")

    def test_a_warn_percent_above_one_hundred_is_refused(self) -> None:
        with self.assertRaises(budgets.BudgetError) as caught:
            self.define(warn_percent=150)
        self.assertEqual(caught.exception.code, "invalid_warn_percent")


class ReserveTests(unittest.TestCase):
    """The claim is one conditional update, and it is all-or-nothing."""

    def reserve(self, rows, **kwargs):
        cursor = _FakeCursor(rows)
        with (
            mock.patch.object(
                budgets, "connect", return_value=_FakeConnection(cursor)
            ),
            mock.patch.object(budgets, "_count_block"),
        ):
            decision = budgets.reserve(
                settings(), tenant_key="default", module="chat", **kwargs
            )
        return decision, cursor

    def test_no_budget_means_no_claim_and_no_refusal(self) -> None:
        decision, cursor = self.reserve([{"id": TENANT_ID}, None])

        self.assertEqual(decision.outcome, budgets.OK)
        self.assertEqual(decision.claims, ())
        self.assertEqual(len(cursor.statements), 2)

    def test_headroom_produces_a_claim(self) -> None:
        decision, _ = self.reserve(
            [{"id": TENANT_ID}, budget_row(), window_row(spent=100), {"id": WINDOW_ID}]
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(len(decision.claims), 1)
        claim = decision.claims[0]
        self.assertEqual(claim.budget_id, BUDGET_ID)
        self.assertEqual(claim.reserved_micros, budgets.DEFAULT_CLAIM_MICROS)

    def test_a_hard_limit_refuses_when_the_claim_would_not_fit(self) -> None:
        decision, _ = self.reserve(
            [
                {"id": TENANT_ID},
                budget_row(cost_micros_limit=1_000),
                window_row(spent=1_000),
            ]
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.outcome, budgets.BLOCKED)
        self.assertIn("used up", decision.reason)

    def test_an_advisory_budget_warns_instead_of_refusing(self) -> None:
        decision, _ = self.reserve(
            [
                {"id": TENANT_ID},
                budget_row(cost_micros_limit=1_000, hard_stop=False),
                window_row(spent=1_000),
            ]
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.outcome, budgets.WARN)
        self.assertEqual(decision.claims, ())

    def test_the_claim_is_conditional_on_the_remaining_headroom(self) -> None:
        _, cursor = self.reserve(
            [{"id": TENANT_ID}, budget_row(), window_row(), {"id": WINDOW_ID}]
        )

        claim_sql = cursor.statements[-1]
        self.assertIn("spent_micros + reserved_micros + %s::bigint <= %s::bigint",
                      claim_sql)
        self.assertIn("RETURNING id", claim_sql)

    def test_reserved_headroom_counts_against_the_limit(self) -> None:
        decision, _ = self.reserve(
            [
                {"id": TENANT_ID},
                budget_row(cost_micros_limit=1_000),
                window_row(spent=0, reserved=1_000),
            ]
        )
        self.assertFalse(decision.allowed)

    def test_a_warning_threshold_reports_without_blocking(self) -> None:
        decision, _ = self.reserve(
            [
                {"id": TENANT_ID},
                budget_row(cost_micros_limit=100_000, warn_percent=80),
                window_row(spent=85_000),
                {"id": WINDOW_ID},
            ]
        )

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.outcome, budgets.WARN)

    def test_a_lost_race_is_a_refusal_not_a_partial_claim(self) -> None:
        decision, _ = self.reserve(
            [{"id": TENANT_ID}, budget_row(), window_row(), None]
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.claims, ())


class SettleTests(unittest.TestCase):
    """A claim is released and replaced by the real cost — even on failure."""

    def settle(self, decision, **kwargs):
        cursor = _FakeCursor([None])
        with mock.patch.object(
            budgets, "connect", return_value=_FakeConnection(cursor)
        ):
            budgets.settle(settings(), decision, **kwargs)
        return cursor

    def claim(self) -> budgets.Decision:
        return budgets.Decision(
            outcome=budgets.OK,
            claims=(
                budgets.Claim(
                    budget_id=BUDGET_ID,
                    window_id=WINDOW_ID,
                    reserved_micros=10_000,
                    reserved_tokens=0,
                ),
            ),
        )

    def test_settlement_releases_the_claim_and_charges_the_cost(self) -> None:
        cursor = self.settle(self.claim(), actual_micros=4_000, actual_tokens=120)

        statement = cursor.statements[0]
        self.assertIn("reserved_micros - %s", statement)
        self.assertIn("spent_micros + %s", statement)
        self.assertEqual(cursor.params[0][2], 4_000)

    def test_a_failed_call_still_frees_its_headroom(self) -> None:
        cursor = self.settle(self.claim(), actual_micros=0, actual_tokens=0)

        self.assertIn("reserved_micros - %s", cursor.statements[0])
        self.assertEqual(cursor.params[0][2], 0)

    def test_nothing_is_written_when_nothing_was_claimed(self) -> None:
        cursor = self.settle(budgets.Decision(outcome=budgets.BLOCKED))
        self.assertEqual(cursor.statements, [])


class StatusTests(unittest.TestCase):
    """The admin view needs a state, not a percentage to interpret."""

    def status(self, row):
        cursor = _FakeCursor([{"id": TENANT_ID}, row])
        with mock.patch.object(
            budgets, "connect", return_value=_FakeConnection(cursor)
        ):
            return budgets.status(settings(), tenant_key="default")

    def test_an_untouched_budget_is_ok(self) -> None:
        body = self.status(budget_row(spent_micros=0, reserved_micros=0))
        self.assertEqual(body["budgets"][0]["state"], "ok")

    def test_crossing_the_warn_threshold_is_warning(self) -> None:
        body = self.status(budget_row(spent_micros=900_000))
        self.assertEqual(body["budgets"][0]["state"], "warning")

    def test_a_full_hard_budget_is_exhausted(self) -> None:
        body = self.status(budget_row(spent_micros=1_000_000))
        self.assertEqual(body["budgets"][0]["state"], "exhausted")

    def test_a_full_advisory_budget_is_over_not_exhausted(self) -> None:
        body = self.status(
            budget_row(spent_micros=1_000_000, hard_stop=False)
        )
        self.assertEqual(body["budgets"][0]["state"], "over")

    def test_a_disabled_budget_says_so(self) -> None:
        body = self.status(budget_row(enabled=False))
        self.assertEqual(body["budgets"][0]["state"], "disabled")


class GuardTests(unittest.TestCase):
    """The guard is what the router brackets a call with."""

    def test_a_blocked_reserve_raises_before_the_call(self) -> None:
        guard = budgets.Guard(settings(), budgets_ctx())
        blocked = budgets.Decision(outcome=budgets.BLOCKED, reason="no headroom")
        with (
            mock.patch.object(budgets, "reserve", return_value=blocked),
            self.assertRaises(budgets.BudgetError) as caught,
        ):
            guard.open()
        self.assertEqual(caught.exception.code, "budget_exhausted")

    def test_a_warning_is_announced_but_allowed(self) -> None:
        announce = mock.Mock()
        guard = budgets.Guard(settings(), budgets_ctx(), announce=announce)
        warned = budgets.Decision(outcome=budgets.WARN, reason="almost there")
        with mock.patch.object(budgets, "reserve", return_value=warned):
            guard.open()
        announce.assert_called_once()

    def test_an_allowed_call_is_not_announced(self) -> None:
        announce = mock.Mock()
        guard = budgets.Guard(settings(), budgets_ctx(), announce=announce)
        with mock.patch.object(
            budgets, "reserve", return_value=budgets.Decision(outcome=budgets.OK)
        ):
            guard.open()
        announce.assert_not_called()

    def test_a_broken_guard_fails_open(self) -> None:
        guard = budgets.Guard(settings(), budgets_ctx())
        with mock.patch.object(
            budgets, "reserve", side_effect=RuntimeError("database gone")
        ):
            guard.open()  # must not raise: AI keeps working

    def test_closing_settles_the_claim_with_the_real_cost(self) -> None:
        decision = budgets.Decision(
            outcome=budgets.OK,
            claims=(budgets.Claim(BUDGET_ID, WINDOW_ID, 10_000, 0),),
        )
        guard = budgets.Guard(settings(), budgets_ctx())
        with (
            mock.patch.object(budgets, "reserve", return_value=decision),
            mock.patch.object(budgets, "settle") as settle,
            mock.patch.object(usage, "cost_of", return_value=(1_234, 15)),
        ):
            guard.open()
            guard.close(
                (
                    AttemptSummary(
                        provider="google",
                        model="gemini-2.5-flash",
                        outcome="succeeded",
                        usage=TokenUsage(10, 0, 5, None, 15, "reported"),
                    ),
                )
            )

        settle.assert_called_once()
        self.assertEqual(settle.call_args.kwargs["actual_micros"], 1_234)
        self.assertEqual(settle.call_args.kwargs["actual_tokens"], 15)

    def test_closing_twice_settles_once(self) -> None:
        decision = budgets.Decision(
            outcome=budgets.OK,
            claims=(budgets.Claim(BUDGET_ID, WINDOW_ID, 10_000, 0),),
        )
        guard = budgets.Guard(settings(), budgets_ctx())
        with (
            mock.patch.object(budgets, "reserve", return_value=decision),
            mock.patch.object(budgets, "settle") as settle,
            mock.patch.object(usage, "cost_of", return_value=(0, 0)),
        ):
            guard.open()
            guard.close(())
            guard.close(())
        self.assertEqual(settle.call_count, 1)


def budgets_ctx() -> object:
    return type(
        "Ctx",
        (),
        {"module": "chat", "superset_user_id": 7, "tenant_key": "default"},
    )()


if __name__ == "__main__":
    unittest.main()
