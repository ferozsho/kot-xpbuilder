"""Tests for the candidate cache (P4-F11).

The key is the safety mechanism: a cached statement may only be reused when the
question, the schema, the row-level policy, the dialect and the row cap all match.
Redis is exercised live (§7.30); these tests pin the key and the value contract.
"""

from __future__ import annotations

import unittest

from xp_prompt import cache, schema_slice


def dataset(**overrides):
    entry = {
        "name": "Student Overview",
        "schema": "public",
        "database": "File uploads",
        "dialect": "postgres",
        "sql": "SELECT student_id FROM public.students",
        "columns": [{"name": "student_id", "type": "STRING"}],
    }
    entry.update(overrides)
    return entry


def slice_(datasets=None):
    return schema_slice.SchemaSlice.from_payload(
        {"datasets": datasets if datasets is not None else [dataset()]}
    )


def key_for(question="How many students?", schema=None, **overrides):
    arguments = {
        "tenant_key": "default",
        "question": question,
        "schema": schema or slice_(),
        "dialect": "postgres",
        "max_rows": 200,
    }
    arguments.update(overrides)
    return cache.cache_key(**arguments)


class KeyTest(unittest.TestCase):
    def test_the_same_question_and_schema_give_the_same_key(self) -> None:
        self.assertEqual(key_for(), key_for())

    def test_whitespace_and_case_do_not_matter(self) -> None:
        self.assertEqual(key_for("How many students?"), key_for("  how   MANY students? "))

    def test_a_different_question_is_a_different_key(self) -> None:
        self.assertNotEqual(key_for("How many students?"), key_for("How many courses?"))

    def test_a_changed_schema_is_a_different_key(self) -> None:
        before = key_for()
        after = key_for(schema=slice_([dataset(columns=[{"name": "student_id"}])]))

        self.assertNotEqual(before, after)

    def test_a_changed_dataset_definition_is_a_different_key(self) -> None:
        before = key_for()
        after = key_for(
            schema=slice_([dataset(sql="SELECT student_id, tenant_id FROM public.students")])
        )

        self.assertNotEqual(before, after)

    def test_a_changed_policy_is_a_different_key(self) -> None:
        """A cached statement must not survive a row-level policy change."""
        before = key_for()
        after = key_for(schema=slice_([dataset(rls=["tenant_id = 7"])]))

        self.assertNotEqual(before, after)

    def test_the_dialect_and_the_cap_are_part_of_the_key(self) -> None:
        self.assertNotEqual(key_for(), key_for(dialect="mysql"))
        self.assertNotEqual(key_for(), key_for(max_rows=1_000))

    def test_the_tenant_is_part_of_the_key(self) -> None:
        self.assertNotEqual(key_for(), key_for(tenant_key="other"))

    def test_the_key_is_namespaced(self) -> None:
        self.assertTrue(key_for().startswith(f"{cache.CACHE_PREFIX}:default:"))


class ValueTest(unittest.TestCase):
    def candidate(self, **overrides):
        fields = {
            "sql": "SELECT COUNT(*) FROM \"Student Overview\"",
            "dialect": "postgres",
            "rationale": "one metric",
            "assumptions": ("one row per student",),
            "tables": ("student overview",),
            "limit": 200,
            "fingerprint": "f" * 64,
            "provider": "gemini",
            "model": "gemini-2.5-flash",
            "correlation_id": "abc",
        }
        fields.update(overrides)
        return cache.CachedCandidate(**fields)

    def test_a_round_trip_is_lossless(self) -> None:
        original = self.candidate()

        restored = cache.CachedCandidate.from_payload(original.to_payload())

        self.assertIsNotNone(restored)
        self.assertEqual(restored.sql, original.sql)
        self.assertEqual(restored.assumptions, original.assumptions)
        self.assertEqual(restored.provider, "gemini")

    def test_a_stale_version_is_a_miss(self) -> None:
        payload = self.candidate().to_payload()
        payload["version"] = "0"

        self.assertIsNone(cache.CachedCandidate.from_payload(payload))

    def test_a_broken_entry_is_a_miss(self) -> None:
        for payload in ({}, {"version": cache.CACHE_VERSION}, "nope", None):
            with self.subTest(payload=payload):
                self.assertIsNone(cache.CachedCandidate.from_payload(payload))

    def test_an_entry_without_sql_or_dialect_is_a_miss(self) -> None:
        payload = self.candidate().to_payload()
        payload["sql"] = "  "

        self.assertIsNone(cache.CachedCandidate.from_payload(payload))


class SettingsTest(unittest.TestCase):
    class FakeSettings:
        candidate_cache_enabled = True
        candidate_cache_ttl_seconds = 600

    def test_enabled_follows_the_setting_and_the_ttl(self) -> None:
        self.assertTrue(cache.enabled(self.FakeSettings()))

        class Disabled:
            candidate_cache_enabled = False
            candidate_cache_ttl_seconds = 600

        class Expired:
            candidate_cache_enabled = True
            candidate_cache_ttl_seconds = 0

        self.assertFalse(cache.enabled(Disabled()))
        self.assertFalse(cache.enabled(Expired()))

    def test_the_ttl_is_clamped(self) -> None:
        class TooLong:
            candidate_cache_enabled = True
            candidate_cache_ttl_seconds = 10_000_000

        class Garbage:
            candidate_cache_enabled = True
            candidate_cache_ttl_seconds = "forever"

        self.assertEqual(cache.ttl_seconds(TooLong()), cache.MAX_TTL_SECONDS)
        self.assertEqual(cache.ttl_seconds(Garbage()), cache.DEFAULT_TTL_SECONDS)


if __name__ == "__main__":
    unittest.main()
