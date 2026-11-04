"""Tests for provider billing imports and the estimate-versus-billed variance.

The two properties that matter: an invoice never rewrites the estimate, and a
re-imported export does not double the numbers an operator is looking at.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from typing import Self
from unittest import mock

from xp_prompt import billing
from xp_prompt.config import Settings

ROOT_SECRET = "unit-test-root-secret-value"
TENANT_ID = "11111111-1111-1111-1111-111111111111"
IMPORT_ID = "22222222-2222-2222-2222-222222222222"


def _settings(**overrides) -> Settings:
    """Settings with optional billing-source fields, built from the same fixture."""
    base = {
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
    if overrides.get("billing_source_url"):
        base["XP_PROMPT_BILLING_SOURCE_URL"] = overrides["billing_source_url"]
    if overrides.get("billing_source_token"):
        base["XP_PROMPT_BILLING_SOURCE_TOKEN"] = overrides["billing_source_token"]
    return Settings.from_env(base)


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
    def __init__(self, rows: list[dict | None] | None = None) -> None:
        self._rows = list(rows or [])
        self.statements: list[str] = []
        self.params: list[object] = []
        self._current: dict | None = None
        self._all: list = []

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
        # A scripted list means "this query returned several rows".
        if isinstance(self._current, list):
            return self._current
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


def import_row(**overrides) -> dict:
    row = {
        "id": IMPORT_ID,
        "provider": "Google Gemini",
        "period_start": datetime(2026, 11, 1, tzinfo=UTC),
        "period_end": datetime(2026, 11, 2, tzinfo=UTC),
        "currency": "USD",
        "source": "console-export",
        "row_count": 2,
        "total_actual_micros": 5_000,
        "created_at": datetime(2026, 11, 4, tzinfo=UTC),
    }
    row.update(overrides)
    return row


def period() -> tuple[datetime, datetime]:
    return (
        datetime(2026, 11, 1, tzinfo=UTC),
        datetime(2026, 11, 2, tzinfo=UTC),
    )


class RowTests(unittest.TestCase):
    def test_a_row_needs_an_integer_amount(self) -> None:
        with self.assertRaises(billing.BillingError) as caught:
            billing.Line.parse({"model": "m", "actual_micros": "1.5"})
        self.assertEqual(caught.exception.code, "invalid_row")

    def test_a_row_needs_a_model(self) -> None:
        with self.assertRaises(billing.BillingError):
            billing.Line.parse({"actual_micros": 10})

    def test_a_negative_amount_is_refused(self) -> None:
        with self.assertRaises(billing.BillingError):
            billing.Line.parse({"model": "m", "actual_micros": -1})

    def test_an_absent_request_id_is_none_not_empty(self) -> None:
        line = billing.Line.parse({"model": "m", "actual_micros": 10})
        self.assertIsNone(line.provider_request_id)


class ImportTests(unittest.TestCase):
    def record(self, rows, script):
        cursor = _FakeCursor([{"id": TENANT_ID}, *script])
        with mock.patch.object(
            billing, "connect", return_value=_FakeConnection(cursor)
        ):
            record, replayed = billing.record_import(
                settings(),
                tenant_key="default",
                provider="Google Gemini",
                period_start=period()[0],
                period_end=period()[1],
                currency="usd",
                rows=rows,
            )
        return record, replayed, cursor

    def rows(self):
        return [
            {"model": "gemini-3.5-flash-lite", "actual_micros": 3_000,
             "provider_request_id": "req-1"},
            {"model": "gemini-3.5-flash-lite", "actual_micros": 2_000},
        ]

    def test_a_currency_is_normalised(self) -> None:
        _, _, cursor = self.record(self.rows(), [import_row(), [], []])
        # params[0] is the tenant lookup; the import INSERT follows.
        insert_params = next(
            params
            for sql, params in zip(cursor.statements, cursor.params, strict=False)
            if "INSERT INTO ai_billing_import" in sql
        )
        self.assertIn("USD", insert_params)

    def test_a_short_currency_is_refused(self) -> None:
        with self.assertRaises(billing.BillingError) as caught:
            billing.record_import(
                settings(),
                tenant_key="default",
                provider="Google Gemini",
                period_start=period()[0],
                period_end=period()[1],
                currency="US",
                rows=self.rows(),
            )
        self.assertEqual(caught.exception.code, "invalid_currency")

    def test_an_import_needs_rows(self) -> None:
        with self.assertRaises(billing.BillingError) as caught:
            billing.record_import(
                settings(),
                tenant_key="default",
                provider="Google Gemini",
                period_start=period()[0],
                period_end=period()[1],
                currency="USD",
                rows=[],
            )
        self.assertEqual(caught.exception.code, "empty_import")

    def test_the_period_must_advance(self) -> None:
        with self.assertRaises(billing.BillingError) as caught:
            billing.record_import(
                settings(),
                tenant_key="default",
                provider="Google Gemini",
                period_start=period()[1],
                period_end=period()[0],
                currency="USD",
                rows=self.rows(),
            )
        self.assertEqual(caught.exception.code, "invalid_period")

    def test_lines_are_written_for_every_row(self) -> None:
        _, _, cursor = self.record(self.rows(), [import_row(), [], []])
        inserts = [sql for sql in cursor.statements if "INSERT INTO ai_billing_line" in sql]
        self.assertEqual(len(inserts), 2)

    def test_matching_is_by_request_id_only(self) -> None:
        _, _, cursor = self.record(self.rows(), [import_row(), [], []])
        match_sql = [
            sql for sql in cursor.statements if "matched_attempt_id = a.id" in sql
        ]
        self.assertEqual(len(match_sql), 1)
        self.assertIn("a.provider_request_id = l.provider_request_id", match_sql[0])
        # No fuzzy matching: a line without a request id stays unmatched.
        self.assertNotIn("l.model = a.model", match_sql[0])

    def test_a_reimport_is_reported_as_a_replay(self) -> None:
        record, replayed, cursor = self.record(
            self.rows(), [None, import_row()]
        )
        self.assertTrue(replayed)
        self.assertEqual(record.id, IMPORT_ID)
        self.assertIn("fingerprint = %s", cursor.statements[-1])

    def test_the_fingerprint_ignores_row_order(self) -> None:
        first = billing._fingerprint(
            "p", [billing.Line.parse(row) for row in self.rows()]
        )
        second = billing._fingerprint(
            "p", [billing.Line.parse(row) for row in reversed(self.rows())]
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)


class VarianceTests(unittest.TestCase):
    def variance(self, totals, estimated, unattributed, models):
        cursor = _FakeCursor(
            [
                {"id": TENANT_ID},
                import_row(),
                totals,
                estimated,
                unattributed,
                models,
            ]
        )
        with mock.patch.object(
            billing, "connect", return_value=_FakeConnection(cursor)
        ):
            return billing.variance(settings(), IMPORT_ID, tenant_key="default")

    def test_the_difference_is_billed_minus_estimated(self) -> None:
        body = self.variance(
            {"lines": 2, "actual_micros": 5_000, "unmatched": 1, "matched": 1},
            {"estimated_micros": 4_000, "matched_attempts": 1,
             "unpriced_attempts": 0},
            {"unattributed": 0},
            [{"model": "m", "lines": 2, "matched": 1, "actual_micros": 5_000,
              "estimated_micros": 4_000}],
        )

        self.assertEqual(body["difference_micros"], 1_000)
        self.assertEqual(body["difference_percent"], 25.0)
        self.assertEqual(body["unmatched_lines"], 1)

    def test_the_report_states_that_the_estimate_is_preserved(self) -> None:
        body = self.variance({}, {}, {}, [])
        self.assertTrue(body["estimate_is_preserved"])

    def test_an_unknown_import_is_refused(self) -> None:
        cursor = _FakeCursor([{"id": TENANT_ID}, None])
        with mock.patch.object(
            billing, "connect", return_value=_FakeConnection(cursor)
        ), self.assertRaises(billing.BillingError) as caught:
            billing.variance(settings(), IMPORT_ID, tenant_key="default")
        self.assertEqual(caught.exception.code, "import_not_found")

    def test_no_estimate_yields_no_percentage(self) -> None:
        body = self.variance(
            {"lines": 1, "actual_micros": 5_000, "unmatched": 1, "matched": 0},
            {"estimated_micros": 0, "matched_attempts": 0,
             "unpriced_attempts": 0},
            {"unattributed": 0},
            [],
        )
        self.assertIsNone(body["difference_percent"])

    def test_unattributed_attempts_are_counted_separately(self) -> None:
        body = self.variance(
            {"lines": 1, "actual_micros": 1, "unmatched": 0, "matched": 1},
            {"estimated_micros": 1, "matched_attempts": 1,
             "unpriced_attempts": 0},
            {"unattributed": 3},
            [],
        )
        self.assertEqual(body["unattributed_attempts"], 3)


class FetchTests(unittest.TestCase):
    """Fetching an export is the one place a URL from settings is dereferenced."""

    def test_a_source_must_be_https(self) -> None:
        with self.assertRaises(billing.BillingError) as caught:
            billing._assert_fetchable("http://example.com/export.json")
        self.assertEqual(caught.exception.code, "unsafe_source")

    def test_a_localhost_source_is_refused(self) -> None:
        for url in (
            "https://localhost/export.json",
            "https://127.0.0.1/export.json",
            "https://metadata.google.internal/computeMetadata/v1/",
        ):
            with self.assertRaises(billing.BillingError) as caught:
                billing._assert_fetchable(url)
            self.assertEqual(caught.exception.code, "unsafe_source", url)

    def test_a_private_address_is_refused(self) -> None:
        for url in ("https://10.0.0.5/x", "https://192.168.1.2/x", "https://[fd00::1]/x"):
            with self.assertRaises(billing.BillingError):
                billing._assert_fetchable(url)

    def test_a_public_host_is_allowed(self) -> None:
        billing._assert_fetchable("https://billing.example.com/export.json")

    def test_an_unconfigured_source_says_so(self) -> None:
        with self.assertRaises(billing.BillingError) as caught:
            billing.fetch_export(_settings(billing_source_url=None), tenant_key="default")
        self.assertEqual(caught.exception.code, "not_configured")

    def test_the_period_defaults_to_the_previous_day(self) -> None:
        start, end = billing._export_period({}, None, None)
        self.assertEqual((end - start).days, 1)
        self.assertEqual(end.utcoffset().total_seconds(), 0)

    def test_the_export_period_wins_over_the_default(self) -> None:
        start, end = billing._export_period(
            {"period_start": "2026-11-01T00:00:00Z", "period_end": "2026-11-02T00:00:00Z"},
            None,
            None,
        )
        self.assertEqual(start.day, 1)
        self.assertEqual(end.day, 2)

    def test_a_naive_timestamp_is_read_as_utc(self) -> None:
        parsed = billing._iso({"period_start": "2026-11-01T00:00:00"}, "period_start")
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_the_source_label_keeps_only_the_host(self) -> None:
        label = billing._source_label("https://billing.example.com/e.json?sig=secret")
        self.assertEqual(label, "fetch:billing.example.com")
        self.assertNotIn("secret", label)

    def test_source_status_never_returns_the_url_or_token(self) -> None:
        # The config parser already refuses credentials, queries and fragments,
        # so the remaining leak to guard against is the token.
        status = billing.source_status(
            _settings(
                billing_source_url="https://billing.example.com/e.json",
                billing_source_token="super-secret",
            )
        )
        self.assertTrue(status["configured"])
        self.assertEqual(status["host"], "billing.example.com")
        self.assertNotIn("super-secret", str(status))

    def test_a_fetched_export_is_imported_like_a_pasted_one(self) -> None:
        body = {
            "provider": "Google Gemini",
            "currency": "USD",
            "period_start": "2026-11-01T00:00:00Z",
            "period_end": "2026-11-02T00:00:00Z",
            "rows": [{"model": "m", "actual_micros": 5}],
        }
        with (
            mock.patch.object(billing, "_get_json", return_value=body),
            mock.patch.object(
                billing,
                "record_import",
                return_value=(billing.Import(
                    id=IMPORT_ID,
                    provider="Google Gemini",
                    period_start=None,
                    period_end=None,
                    currency="USD",
                    source="fetch:billing.example.com",
                    row_count=1,
                    total_actual_micros=5,
                    created_at=None,
                ), False),
            ) as record,
        ):
            outcome = billing.fetch_export(
                _settings(billing_source_url="https://billing.example.com/e.json"),
                tenant_key="default",
            )
        self.assertEqual(outcome["rows"], 1)
        self.assertFalse(outcome["replayed"])
        self.assertEqual(record.call_args.kwargs["provider"], "Google Gemini")

    def test_an_export_without_rows_is_refused(self) -> None:
        with (
            mock.patch.object(billing, "_get_json", return_value={"provider": "x"}),
            self.assertRaises(billing.BillingError) as caught,
        ):
            billing.fetch_export(
                _settings(billing_source_url="https://billing.example.com/e.json"),
                tenant_key="default",
            )
        self.assertEqual(caught.exception.code, "invalid_export")


if __name__ == "__main__":
    unittest.main()
