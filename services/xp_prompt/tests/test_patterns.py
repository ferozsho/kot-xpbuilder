"""Tests for the validated repair pattern library (P5-F02/F03/F08/F09)."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from xp_prompt import patterns
from xp_prompt.config import Settings
from xp_prompt.patterns import Pattern

ROOT_SECRET = "unit-test-root-secret-value"


def settings() -> Settings:
    return Settings.from_env(
        {
            "XP_PROMPT_POSTGRES_HOST": "xp-prompt-db",
            "XP_PROMPT_POSTGRES_PORT": "5432",
            "XP_PROMPT_POSTGRES_USER": "xprompt",
            "XP_PROMPT_POSTGRES_PASSWORD": "database-secret",
            "XP_PROMPT_POSTGRES_DB": "xprompt",
            "XP_PROMPT_REDIS_HOST": "superset-redis",
            "XP_PROMPT_REDIS_PORT": "6379",
            "XP_PROMPT_REDIS_PASSWORD": "redis-secret",
            "XP_PROMPT_REDIS_DB": "2",
            "XP_PROMPT_QDRANT_URL": "http://xp-prompt-qdrant:6333",
            "XP_PROMPT_SERVICE_SECRET": ROOT_SECRET,
        }
    )


def pattern(**overrides) -> Pattern:
    base = dict(
        id="p1",
        state="promoted",
        version=1,
        dialect="postgres",
        kind="unknown_identifier",
        failure_fingerprint="f" * 64,
        schema_fingerprint="s" * 64,
        intent="query",
        correction_sha256="c" * 64,
        successes=3,
        failures=0,
        confidence=0.8,
        validator_version=patterns.VALIDATOR_VERSION,
        policy_version=patterns.POLICY_VERSION,
        updated_at=datetime.now(UTC),
        last_used_at=datetime.now(UTC),
    )
    base.update(overrides)
    return Pattern(**base)


class ConfidenceTest(unittest.TestCase):
    def test_laplace_smoothing_keeps_zero_from_looking_certain(self) -> None:
        self.assertAlmostEqual(patterns.confidence(0, 0), 0.5)
        self.assertGreater(patterns.confidence(1, 0), patterns.confidence(1, 1))
        self.assertAlmostEqual(patterns.confidence(2, 0), 0.75)

    def test_negative_counts_are_ignored(self) -> None:
        self.assertAlmostEqual(patterns.confidence(-3, -4), 0.5)


class CorrectionIdentityTest(unittest.TestCase):
    def test_whitespace_does_not_create_a_second_pattern(self) -> None:
        first = patterns.correction_sha256("select  id\nfrom t")
        second = patterns.correction_sha256("select id from t")
        self.assertEqual(first, second)

    def test_empty_correction_has_no_identity(self) -> None:
        self.assertEqual(patterns.correction_sha256("   "), "")


class RankTest(unittest.TestCase):
    def test_more_successes_outrank_fewer(self) -> None:
        ranked = patterns.rank(
            [
                pattern(id="weak", successes=1, confidence=0.66),
                pattern(id="strong", successes=8, confidence=0.9),
            ]
        )
        self.assertEqual(ranked[0].id, "strong")
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_stale_recency_lowers_the_score(self) -> None:
        fresh = pattern(id="fresh", last_used_at=datetime.now(UTC))
        stale = pattern(
            id="stale", last_used_at=datetime.now(UTC) - timedelta(days=365)
        )
        ranked = patterns.rank([stale, fresh])
        self.assertEqual(ranked[0].id, "fresh")
        self.assertGreater(ranked[0].factors["recency"], ranked[1].factors["recency"])

    def test_an_older_validator_loses_compatibility(self) -> None:
        current = pattern(id="current")
        old = pattern(id="old", validator_version="p3.1")
        ranked = patterns.rank([old, current])
        self.assertEqual(ranked[0].id, "current")
        self.assertEqual(ranked[1].factors["compatibility"], 0.0)

    def test_an_older_policy_loses_compatibility(self) -> None:
        ranked = patterns.rank([pattern(policy_version="0")])
        self.assertEqual(ranked[0].factors["compatibility"], 0.0)

    def test_score_is_explainable(self) -> None:
        ranked = patterns.rank([pattern()])
        self.assertEqual(
            set(ranked[0].factors),
            {"confidence", "history", "recency", "compatibility"},
        )
        self.assertGreater(ranked[0].score, 0.0)


class StateTest(unittest.TestCase):
    def test_a_first_success_validates_but_does_not_promote(self) -> None:
        state = patterns._settled_state("candidate", 1, 0, ["c1"], 2)
        self.assertEqual(state, "validated")

    def test_threshold_and_independence_are_both_required(self) -> None:
        # Two successes from one conversation is one validation, not two.
        self.assertEqual(patterns._settled_state("validated", 2, 0, ["c1"], 2), "validated")
        self.assertEqual(
            patterns._settled_state("validated", 2, 0, ["c1", "c2"], 2), "promoted"
        )

    def test_one_success_promotes_when_the_threshold_is_one(self) -> None:
        self.assertEqual(patterns._settled_state("candidate", 1, 0, ["c1"], 1), "promoted")

    def test_a_promoted_pattern_is_demoted_when_confidence_collapses(self) -> None:
        state = patterns._settled_state("promoted", 2, 6, ["c1", "c2"], 2)
        self.assertEqual(state, "validated")

    def test_no_success_means_validating(self) -> None:
        self.assertEqual(patterns._settled_state("candidate", 0, 1, [], 2), "validating")

    def test_a_disabled_pattern_is_not_revived(self) -> None:
        state = patterns._settled_state("deprecated", 5, 0, ["a", "b", "c"], 1)
        self.assertEqual(state, "deprecated")


class PublicShapeTest(unittest.TestCase):
    def test_the_statement_is_hidden_by_default(self) -> None:
        payload = pattern(sql="select 1").to_public_dict()
        self.assertTrue(payload["sql_hidden"])
        self.assertNotIn("sql", payload)
        self.assertEqual(payload["evidence_count"], 0)

    def test_the_statement_is_returned_only_on_request(self) -> None:
        payload = pattern(sql="select 1").to_public_dict(include_sql=True)
        self.assertFalse(payload["sql_hidden"])
        self.assertEqual(payload["sql"], "select 1")

    def test_promotion_metadata_is_visible(self) -> None:
        payload = pattern(promotion_policy="operator", version=4).to_public_dict()
        self.assertEqual(payload["promotion_policy"], "operator")
        self.assertEqual(payload["version"], 4)
        self.assertEqual(payload["state"], "promoted")


class EmptyMetricsTest(unittest.TestCase):
    def test_an_absent_store_reports_zeroes_rather_than_nothing(self) -> None:
        metrics = patterns._empty_metrics()
        self.assertEqual(set(metrics["states"]), set(patterns.STATES))
        self.assertEqual(metrics["attempts"]["suppressed"], 0)
        self.assertEqual(metrics["usable"], 0)


class StoreFallbackTest(unittest.TestCase):
    """A broken pattern store must degrade reuse, never break a caller."""

    def test_find_returns_nothing_when_the_store_is_unreachable(self) -> None:
        broken = replace(settings(), postgres_host="127.0.0.1")
        found = patterns.find(
            broken,
            tenant_key="default",
            failure_fingerprint="f" * 64,
            dialect="postgres",
            schema_fingerprint="s" * 64,
        )
        self.assertEqual(found, [])

    def test_metrics_return_zeroes_when_the_store_is_unreachable(self) -> None:
        broken = replace(settings(), postgres_host="127.0.0.1")
        metrics = patterns.metrics(broken, tenant_key="default")
        self.assertEqual(metrics["usable"], 0)


if __name__ == "__main__":
    unittest.main()
