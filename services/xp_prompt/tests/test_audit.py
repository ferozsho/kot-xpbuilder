"""Tests for the SQL audit trail (P4-F12).

The database round trip is exercised live (see runtime-evidence §7.28); these tests
pin the two things that must not regress silently: what a record exposes, and how
much a caller is allowed to store.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from xp_prompt import audit


def record(**overrides):
    fields = {
        "id": "11111111-1111-1111-1111-111111111111",
        "conversation_id": "22222222-2222-2222-2222-222222222222",
        "superset_user_id": 3,
        "correlation_id": "abc123",
        "intent": "query",
        "outcome": "answered",
        "stage": "execute",
        "code": "ok",
        "dialect": "postgres",
        "tables": ("public.mdl_user",),
        "sql_sha256": "f" * 64,
        "created_at": datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return audit.AuditRecord(**fields)


class RedactionTest(unittest.TestCase):
    def test_the_statement_is_hidden_by_default(self) -> None:
        payload = record(sql="SELECT 1").to_public_dict()

        self.assertNotIn("sql", payload)
        self.assertTrue(payload["sql_hidden"])

    def test_asking_for_the_statement_when_allowed_reveals_it(self) -> None:
        payload = record(sql="SELECT 1").to_public_dict(include_sql=True)

        self.assertEqual(payload["sql"], "SELECT 1")
        self.assertFalse(payload["sql_hidden"])

    def test_asking_for_it_when_it_was_not_loaded_stays_hidden(self) -> None:
        payload = record(sql=None).to_public_dict(include_sql=True)

        self.assertNotIn("sql", payload)
        self.assertTrue(payload["sql_hidden"])

    def test_metadata_is_always_present(self) -> None:
        payload = record(sql="SELECT 1").to_public_dict()

        self.assertEqual(
            sorted(payload),
            [
                "code",
                "conversation_id",
                "correlation_id",
                "created_at",
                "dialect",
                "duration_ms",
                "id",
                "inlined",
                "intent",
                "outcome",
                "plan",
                "returned",
                "rls_applied",
                "row_count",
                "sql_hidden",
                "sql_sha256",
                "stage",
                "superset_user_id",
                "tables",
                "truncated",
            ],
        )
        self.assertEqual(payload["sql_sha256"], "f" * 64)

    def test_a_fingerprint_survives_without_the_statement(self) -> None:
        """The whole point: "was this query attempted?" is answerable without the SQL."""
        payload = record(sql="SELECT 1").to_public_dict()

        self.assertEqual(len(payload["sql_sha256"]), 64)
        self.assertNotIn("SELECT", str(payload))


class BoundsTest(unittest.TestCase):
    def test_only_known_outcomes_and_stages_are_accepted(self) -> None:
        self.assertEqual(set(audit.OUTCOMES), {"answered", "refused", "failed"})
        self.assertIn("plan", audit.STAGES)
        self.assertIn("guard", audit.STAGES)

    def test_a_list_is_bounded_and_deduplicated(self) -> None:
        items = audit._json_items(
            [f"table_{index}" for index in range(audit.MAX_TABLES + 10)],
            audit.MAX_TABLES,
            audit.MAX_TABLE_CHARS,
        )

        self.assertEqual(len(items), audit.MAX_TABLES)

        deduplicated = audit._json_items(
            ["a", "a", "b"], audit.MAX_LIST_ITEMS, audit.MAX_ITEM_CHARS
        )
        self.assertEqual(deduplicated, ["a", "b"])

    def test_long_items_are_cut(self) -> None:
        items = audit._json_items(["x" * 500], 5, audit.MAX_ITEM_CHARS)

        self.assertEqual(len(items[0]), audit.MAX_ITEM_CHARS)

    def test_a_non_list_is_empty_not_an_error(self) -> None:
        self.assertEqual(audit._json_items("nope", 5, 10), [])
        self.assertEqual(audit._json_items(None, 5, 10), [])

    def test_optional_numbers_degrade_to_none(self) -> None:
        self.assertIsNone(audit._optional_int("x"))
        self.assertIsNone(audit._optional_float(None))
        self.assertEqual(audit._optional_int("12"), 12)


class RecordHelperTest(unittest.TestCase):
    def test_the_record_helper_is_bounded_and_keeps_a_fingerprint(self) -> None:
        """The stored statement never round-trips as plain text through the record."""
        built = record(sql_sha256="a" * 64)

        self.assertEqual(built.to_public_dict()["sql_sha256"], "a" * 64)
        self.assertIsNone(built.sql)


if __name__ == "__main__":
    unittest.main()
