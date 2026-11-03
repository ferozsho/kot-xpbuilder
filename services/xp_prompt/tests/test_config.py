"""Tests for XP Prompt environment configuration."""

from __future__ import annotations

import unittest

from xp_prompt.config import Settings


class SettingsTest(unittest.TestCase):
    def base_env(self) -> dict[str, str]:
        return {
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
            "XP_PROMPT_DEPENDENCY_TIMEOUT_SECONDS": "3",
        }

    def test_settings_parse_expected_values(self) -> None:
        settings = Settings.from_env(self.base_env())

        self.assertEqual(settings.postgres_host, "xp-prompt-db")
        self.assertEqual(settings.redis_database, 2)
        self.assertEqual(settings.qdrant_url, "http://xp-prompt-qdrant:6333")

    def test_safe_summary_excludes_secrets(self) -> None:
        settings = Settings.from_env(self.base_env())

        summary = settings.safe_summary()

        self.assertNotIn("postgres_password", summary)
        self.assertNotIn("redis_password", summary)
        self.assertNotIn("database-secret", repr(summary))
        self.assertNotIn("redis-secret", repr(summary))
        self.assertNotIn("database-secret", repr(settings))
        self.assertNotIn("redis-secret", repr(settings))

    def test_missing_secret_fails_closed(self) -> None:
        env = self.base_env()
        del env["XP_PROMPT_REDIS_PASSWORD"]

        with self.assertRaisesRegex(RuntimeError, "XP_PROMPT_REDIS_PASSWORD"):
            Settings.from_env(env)

    def test_qdrant_url_rejects_credentials(self) -> None:
        env = self.base_env()
        env["XP_PROMPT_QDRANT_URL"] = "http://user:password@qdrant:6333"

        with self.assertRaisesRegex(RuntimeError, "must not contain credentials"):
            Settings.from_env(env)


if __name__ == "__main__":
    unittest.main()

