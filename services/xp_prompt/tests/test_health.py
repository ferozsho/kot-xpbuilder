"""Tests for XP Prompt readiness aggregation."""

from __future__ import annotations

import unittest

from xp_prompt.config import Settings
from xp_prompt.health import CheckResult, readiness


class ReadinessTest(unittest.TestCase):
    def settings(self) -> Settings:
        return Settings.from_env(
            {
                "XP_PROMPT_POSTGRES_HOST": "xp-prompt-db",
                "XP_PROMPT_POSTGRES_USER": "xprompt",
                "XP_PROMPT_POSTGRES_PASSWORD": "database-secret",
                "XP_PROMPT_POSTGRES_DB": "xprompt",
                "XP_PROMPT_REDIS_HOST": "superset-redis",
                "XP_PROMPT_REDIS_PASSWORD": "redis-secret",
                "XP_PROMPT_QDRANT_URL": "http://xp-prompt-qdrant:6333",
            }
        )

    def test_readiness_is_true_when_all_dependencies_are_ready(self) -> None:
        checks = {
            "postgres": lambda _: CheckResult("postgres", "ready", 1),
            "redis": lambda _: CheckResult("redis", "ready", 1),
            "qdrant": lambda _: CheckResult("qdrant", "ready", 1),
        }

        ready, details = readiness(self.settings(), checks)

        self.assertTrue(ready)
        self.assertEqual(details["qdrant"]["status"], "ready")

    def test_readiness_is_false_when_a_dependency_is_unavailable(self) -> None:
        checks = {
            "postgres": lambda _: CheckResult("postgres", "ready", 1),
            "redis": lambda _: CheckResult(
                "redis", "unavailable", 2, "ConnectionError"
            ),
            "qdrant": lambda _: CheckResult("qdrant", "ready", 1),
        }

        ready, details = readiness(self.settings(), checks)

        self.assertFalse(ready)
        self.assertEqual(details["redis"]["error_class"], "ConnectionError")


if __name__ == "__main__":
    unittest.main()

