"""Tests for the usage ledger: pricing, labelling, idempotency and aggregates.

The two things that go wrong silently in a cost ledger are a float creeping into
money and a missing token count being treated as zero. Both are tested directly,
because neither shows up until a total is wrong in the confident direction.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import Self
from unittest import mock

from xp_prompt import usage
from xp_prompt.config import Settings
from xp_prompt.providers.types import AttemptSummary, TokenUsage

ROOT_SECRET = "unit-test-root-secret-value"
TENANT_ID = "11111111-1111-1111-1111-111111111111"


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


def price(**overrides) -> usage.ModelPrice:
    """$1 per 1M input, $0.25 per 1M cached, $4 per 1M output."""
    fields = {
        "provider": "google",
        "model": "gemini-2.5-flash",
        "version": 1,
        "currency": "usd",
        "unit_tokens": 1_000_000,
        "input_micros_per_unit": 1_000_000,
        "cached_input_micros_per_unit": 250_000,
        "output_micros_per_unit": 4_000_000,
        "reasoning_micros_per_unit": None,
        "source": "list-price",
    }
    fields.update(overrides)
    return usage.ModelPrice(**fields)


def price_row() -> dict:
    return {
        "provider": "google",
        "model": "gemini-2.5-flash",
        "version": 3,
        "currency": "usd",
        "unit_tokens": 1_000_000,
        "input_micros_per_unit": 1_000_000,
        "cached_input_micros_per_unit": 250_000,
        "output_micros_per_unit": 4_000_000,
        "reasoning_micros_per_unit": None,
        "effective_from": datetime(2026, 1, 1, tzinfo=UTC),
        "effective_to": None,
        "source": "list-price",
        "source_url": "",
    }


class _FakeCursor:
    def __init__(self, rows: list[dict | None] | None = None) -> None:
        self._rows = list(rows or [])
        self.statements: list[str] = []
        self.params: list[object] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object = None) -> None:
        self.statements.append(sql)
        self.params.append(params)
        self._current = self._rows.pop(0) if self._rows else None

    def fetchone(self) -> dict | None:
        return getattr(self, "_current", None)

    def fetchall(self) -> list:
        row = getattr(self, "_current", None)
        return [row] if row else []


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _FakeCursor:
        return self._cursor


class CostTests(unittest.TestCase):
    """Integer micros, cached and reasoning discounts, no fabricated precision."""

    def cost(self, **counts) -> int | None:
        return usage.estimate_cost_micros(
            price(),
            input_tokens=counts.get("input_tokens"),
            cached_input_tokens=counts.get("cached_input_tokens"),
            output_tokens=counts.get("output_tokens"),
            reasoning_tokens=counts.get("reasoning_tokens"),
        )

    def test_a_million_input_tokens_costs_one_dollar(self) -> None:
        self.assertEqual(self.cost(input_tokens=1_000_000), 1_000_000)

    def test_the_cached_part_is_priced_at_the_cached_rate(self) -> None:
        # 600k billable at $1/M plus 400k cached at $0.25/M = $0.70
        self.assertEqual(
            self.cost(input_tokens=1_000_000, cached_input_tokens=400_000), 700_000
        )

    def test_every_category_is_priced(self) -> None:
        # 600k in + 400k cached + 200k out + 100k reasoning (output rate)
        self.assertEqual(
            self.cost(
                input_tokens=1_000_000,
                cached_input_tokens=400_000,
                output_tokens=200_000,
                reasoning_tokens=100_000,
            ),
            1_900_000,
        )

    def test_a_missing_cached_rate_falls_back_to_the_input_rate(self) -> None:
        cost = usage.estimate_cost_micros(
            price(cached_input_micros_per_unit=None),
            input_tokens=1_000_000,
            cached_input_tokens=400_000,
            output_tokens=None,
            reasoning_tokens=None,
        )
        self.assertEqual(cost, 1_000_000)

    def test_a_missing_reasoning_rate_falls_back_to_the_output_rate(self) -> None:
        cost = usage.estimate_cost_micros(
            price(reasoning_micros_per_unit=None),
            input_tokens=None,
            cached_input_tokens=None,
            output_tokens=None,
            reasoning_tokens=250_000,
        )
        self.assertEqual(cost, 1_000_000)

    def test_no_usage_is_unknown_not_free(self) -> None:
        self.assertIsNone(self.cost())

    def test_no_price_means_no_estimate(self) -> None:
        cost = usage.estimate_cost_micros(
            None,
            input_tokens=1_000_000,
            cached_input_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
        )
        self.assertIsNone(cost)

    def test_rounding_is_half_up_on_integers(self) -> None:
        coin_price = price(unit_tokens=1_000, input_micros_per_unit=1)
        cost = usage.estimate_cost_micros(
            coin_price,
            input_tokens=1,
            cached_input_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
        )
        # 1 token at 1 micro per 1000 tokens rounds to 0, not to a float.
        self.assertEqual(cost, 0)
        rounded = usage.estimate_cost_micros(
            coin_price,
            input_tokens=600,
            cached_input_tokens=None,
            output_tokens=None,
            reasoning_tokens=None,
        )
        # 600/1000 = 0.6 micro rounds up to 1.
        self.assertEqual(rounded, 1)

    def test_a_cached_count_never_exceeds_the_prompt_count(self) -> None:
        # A provider that reports only cached tokens must not yield a negative part.
        self.assertEqual(
            self.cost(input_tokens=None, cached_input_tokens=500_000), 125_000
        )
        self.assertEqual(
            self.cost(input_tokens=100_000, cached_input_tokens=500_000), 125_000
        )


class MappingTests(unittest.TestCase):
    """The router's summary becomes a row without guessing anything (F02/F03)."""

    def attempt(self, summary: AttemptSummary) -> usage.Attempt:
        return usage._attempt_from_summary(
            summary, usage.Context(tenant_key="default", module="chat").normalized()
        )

    def test_reported_usage_is_labelled_reported(self) -> None:
        row = self.attempt(
            AttemptSummary(
                provider="google",
                model="gemini-2.5-flash",
                outcome="succeeded",
                correlation_id="abc",
                operation="chat_turn",
                usage=TokenUsage(
                    input_tokens=1200,
                    cached_input_tokens=0,
                    output_tokens=300,
                    reasoning_tokens=None,
                    total_tokens=1500,
                    source="reported",
                ),
                duration_ms=850,
                request_id="req-1",
            )
        )
        self.assertEqual(row.usage_source, "reported")
        self.assertEqual(row.input_tokens, 1200)
        self.assertEqual(row.output_tokens, 300)
        self.assertEqual(row.duration_ms, 850)
        self.assertEqual(row.provider_request_id, "req-1")

    def test_missing_usage_stays_missing(self) -> None:
        row = self.attempt(
            AttemptSummary(
                provider="google",
                model="gemini-2.5-flash",
                outcome="failed",
                error_code="http_402",
                error_detail="prepayment credits are depleted",
            )
        )
        self.assertEqual(row.usage_source, "missing")
        self.assertIsNone(row.input_tokens)
        self.assertIsNone(row.output_tokens)
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.error_class, "http_402")

    def test_a_fallback_attempt_is_marked_as_one(self) -> None:
        row = self.attempt(
            AttemptSummary(
                provider="deepseek",
                model="deepseek-chat",
                outcome="succeeded",
                attempt=2,
                fallback=True,
            )
        )
        self.assertTrue(row.fallback)
        self.assertEqual(row.attempt_ordinal, 2)

    def test_an_unknown_module_is_normalized_rather_than_stored(self) -> None:
        context = usage.Context(module="whatever").normalized()
        self.assertEqual(context.module, "other")


class RecordTests(unittest.TestCase):
    """One row per attempt, and a replay cannot add a second (F01)."""

    def record(self, rows, summary=None):
        cursor = _FakeCursor(rows)
        with mock.patch.object(
            usage, "connect", return_value=_FakeConnection(cursor)
        ):
            written = usage.record(
                settings(),
                usage.Context(tenant_key="default", module="chat"),
                summary
                or AttemptSummary(
                    provider="google",
                    model="gemini-2.5-flash",
                    outcome="succeeded",
                    correlation_id="abc123",
                    operation="chat_turn",
                    usage=TokenUsage(1_000_000, 0, 0, None, 1_000_000, "reported"),
                ),
            )
        return written, cursor

    def test_recording_is_idempotent_by_constraint(self) -> None:
        written, cursor = self.record([{"id": TENANT_ID}, price_row(), {"id": "row"}])

        self.assertTrue(written)
        statement = cursor.statements[-1]
        self.assertIn("INSERT INTO ai_provider_attempt", statement)
        self.assertIn("ON CONFLICT", statement)
        self.assertIn("DO NOTHING", statement)
        self.assertIn("correlation_id <> ''", statement)

    def test_a_replay_reports_that_nothing_was_written(self) -> None:
        written, _ = self.record([{"id": TENANT_ID}, price_row(), None])
        self.assertFalse(written)

    def test_the_price_version_is_snapshotted_on_the_row(self) -> None:
        _, cursor = self.record([{"id": TENANT_ID}, price_row(), {"id": "row"}])
        params = cursor.params[-1]
        # The version and the currency travel with the attempt, so a later price
        # edit cannot change what this call cost (F05).
        self.assertIn(3, params)
        self.assertIn("usd", params)

    def test_an_unpriced_model_records_no_cost(self) -> None:
        _, cursor = self.record([{"id": TENANT_ID}, None, {"id": "row"}])
        params = cursor.params[-1]
        self.assertIn(None, params)  # price_version
        cost_index = params.index(None)
        self.assertIsNone(params[cost_index])

    def test_an_unprovisioned_tenant_is_refused(self) -> None:
        cursor = _FakeCursor([None])
        with mock.patch.object(
            usage, "connect", return_value=_FakeConnection(cursor)
        ), self.assertRaises(usage.UsageError):
            usage.record(
                settings(), usage.Context(tenant_key="ghost"), _summary()
            )


class AggregateTests(unittest.TestCase):
    """Totals combine without turning an unknown cost into zero (F07)."""

    def test_an_unknown_cost_stays_unknown_in_the_total(self) -> None:
        totals = usage._combine(
            [
                usage._totals({"requests": 2, "cost_micros": 500}),
                usage._totals({"requests": 3, "cost_micros": None}),
            ],
            ["provider"],
        )
        self.assertEqual(totals["requests"], 5)
        self.assertEqual(totals["estimated_cost_micros"], 500)

    def test_all_unknown_costs_totals_to_unknown(self) -> None:
        totals = usage._combine([usage._totals({"requests": 1, "cost_micros": None})], [])
        self.assertIsNone(totals["estimated_cost_micros"])

    def test_an_empty_ledger_totals_to_zero_requests(self) -> None:
        totals = usage._combine([], [])
        self.assertEqual(totals["requests"], 0)
        self.assertIsNone(totals["estimated_cost_micros"])

    def test_an_unsupported_grouping_column_is_dropped(self) -> None:
        cursor = _FakeCursor([None])
        with mock.patch.object(
            usage, "connect", return_value=_FakeConnection(cursor)
        ):
            body = usage.summary(
                settings(), group_by=["provider", "error_detail; DROP TABLE x"]
            )
        self.assertEqual(body["group_by"], ["provider"])
        self.assertNotIn("DROP TABLE", cursor.statements[0])

    def test_the_summary_labels_its_cost_as_estimated(self) -> None:
        cursor = _FakeCursor([{"requests": 1}])
        with mock.patch.object(
            usage, "connect", return_value=_FakeConnection(cursor)
        ):
            body = usage.summary(settings())
        self.assertTrue(body["cost_is_estimated"])


class PriceValidationTests(unittest.TestCase):
    def test_a_price_needs_a_currency_code(self) -> None:
        with self.assertRaises(usage.UsageError) as caught:
            usage.publish_price(settings(), price(currency="us"))
        self.assertEqual(caught.exception.code, "invalid_price")

    def test_a_negative_rate_is_refused(self) -> None:
        with self.assertRaises(usage.UsageError):
            usage.publish_price(settings(), price(output_micros_per_unit=-1))

    def test_a_price_needs_a_model(self) -> None:
        with self.assertRaises(usage.UsageError):
            usage.publish_price(settings(), price(model="  "))


class ReconciliationTests(unittest.TestCase):
    """The ledger can say whether its own totals are trustworthy (F10)."""

    def reconcile(self, ledger: dict, gaps: list | None = None):
        cursor = _FakeCursor(
            [{"id": TENANT_ID}, ledger, {"providers": 1}, *(gaps or [])]
        )
        with mock.patch.object(
            usage, "connect", return_value=_FakeConnection(cursor)
        ):
            return usage.reconcile(settings())

    def test_a_clean_ledger_is_healthy(self) -> None:
        body = self.reconcile(
            {"attempts": 10, "missing_usage": 2, "unpriced": 0,
             "negative_tokens": 0, "unfinished": 0, "inconsistent_totals": 0}
        )
        self.assertTrue(body["healthy"])
        self.assertEqual(body["attempts"], 10)

    def test_negative_tokens_make_the_ledger_unhealthy(self) -> None:
        body = self.reconcile({"attempts": 1, "negative_tokens": 1})
        self.assertFalse(body["healthy"])

    def test_an_unfinished_attempt_makes_the_ledger_unhealthy(self) -> None:
        body = self.reconcile({"attempts": 1, "unfinished": 1})
        self.assertFalse(body["healthy"])

    def test_price_gaps_are_listed_by_model(self) -> None:
        body = self.reconcile(
            {"attempts": 1},
            gaps=[{"provider": "google", "model": "gemini-2.5-flash"}],
        )
        self.assertEqual(body["price_gaps"], ["google/gemini-2.5-flash"])


def _summary() -> AttemptSummary:
    return AttemptSummary(
        provider="google", model="gemini-2.5-flash", outcome="succeeded"
    )


if __name__ == "__main__":
    unittest.main()
