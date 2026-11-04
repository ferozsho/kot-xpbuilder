"""Tests for the typed settings registry: bounds, ceilings, scope and audit.

Three properties carry the phase, and each has a failure mode that is silent:

* a value outside its range must be refused, not clamped — a clamped timeout looks
  applied while quietly ignoring the operator;
* a hard ceiling must hold even when the stored row is edited directly, because a
  "default" is not a security control;
* a rejected batch must write nothing, or a form with one bad field becomes a
  partial save nobody can reason about.
"""

from __future__ import annotations

import json
import unittest
from typing import Self
from unittest import mock

from xp_prompt import settings_registry as sr
from xp_prompt.config import Settings

ROOT_SECRET = "unit-test-root-secret-value"
TENANT_ID = "11111111-1111-1111-1111-111111111111"
AUDIT_ID = "22222222-2222-2222-2222-222222222222"


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


class RegistryTests(unittest.TestCase):
    """Every key is unique and every default satisfies its own bounds."""

    def test_keys_are_unique(self) -> None:
        keys = [setting.key for setting in sr.REGISTRY]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_default_is_valid(self) -> None:
        for setting in sr.REGISTRY:
            # A default outside its own bounds would make "unset" undefined.
            self.assertEqual(setting.coerce(setting.default), setting.default, setting.key)

    def test_no_setting_may_name_a_secret(self) -> None:
        for setting in sr.REGISTRY:
            segments = setting.key.split(".")
            self.assertFalse(
                any(sr._SECRET_SEGMENT.match(segment) for segment in segments),
                f"{setting.key} looks secret",
            )

    def test_every_setting_declares_an_owner(self) -> None:
        for setting in sr.REGISTRY:
            self.assertTrue(setting.owner, setting.key)

    def test_the_public_payload_explains_a_restart(self) -> None:
        payload = sr.registry_payload()
        self.assertTrue(all("restart_required" in entry for entry in payload))
        self.assertTrue(all("dynamic" in entry for entry in payload))


class CoercionTests(unittest.TestCase):
    def test_an_integer_out_of_range_is_refused_not_clamped(self) -> None:
        setting = sr.BY_KEY["sql.timeout_ms"]
        with self.assertRaises(sr.SettingError) as caught:
            setting.coerce(10_000_000)
        self.assertEqual(caught.exception.code, "out_of_range")

    def test_a_ceiling_is_named_in_the_refusal(self) -> None:
        setting = sr.BY_KEY["sql.max_rows"]
        with self.assertRaises(sr.SettingError) as caught:
            setting.coerce(setting.maximum + 1)
        self.assertIn("platform ceiling", str(caught.exception))

    def test_a_minimum_is_enforced(self) -> None:
        with self.assertRaises(sr.SettingError):
            sr.BY_KEY["provider.max_attempts_per_provider"].coerce(0)

    def test_booleans_accept_the_usual_strings(self) -> None:
        setting = sr.BY_KEY["feature.chat"]
        self.assertTrue(setting.coerce("true"))
        self.assertFalse(setting.coerce("false"))
        self.assertTrue(setting.coerce(True))

    def test_a_non_boolean_is_refused(self) -> None:
        with self.assertRaises(sr.SettingError) as caught:
            sr.BY_KEY["feature.chat"].coerce("maybe")
        self.assertEqual(caught.exception.code, "invalid_type")

    def test_a_boolean_is_not_an_integer(self) -> None:
        # ``True == 1`` in Python, which would make a bool a valid token count.
        with self.assertRaises(sr.SettingError):
            sr.BY_KEY["generation.max_output_tokens"].coerce(True)

    def test_text_is_collapsed_and_bounded(self) -> None:
        setting = sr.BY_KEY["usage.alert_email"]
        self.assertEqual(setting.coerce("  a@b.com   "), "a@b.com")
        with self.assertRaises(sr.SettingError):
            setting.coerce("x" * 600)


class KeyTests(unittest.TestCase):
    def test_an_unknown_key_is_refused(self) -> None:
        with self.assertRaises(sr.SettingError) as caught:
            sr._assert_storable("sql.everything_allowed")
        self.assertEqual(caught.exception.code, "unknown_setting")

    def test_a_secret_key_is_refused_even_if_it_existed(self) -> None:
        with self.assertRaises(sr.SettingError) as caught:
            sr._assert_storable("provider.api_key")
        self.assertEqual(caught.exception.code, "secret_not_allowed")

    def test_a_global_only_setting_cannot_be_set_per_tenant(self) -> None:
        with self.assertRaises(sr.SettingError) as caught:
            sr.apply(
                settings(),
                {"usage.alert_email": "ops@example.com"},
                scope=sr.TENANT,
            )
        self.assertEqual(caught.exception.code, "scope_not_allowed")

    def test_an_empty_batch_is_refused(self) -> None:
        with self.assertRaises(sr.SettingError) as caught:
            sr.apply(settings(), {})
        self.assertEqual(caught.exception.code, "empty_update")

    def test_one_bad_value_writes_nothing(self) -> None:
        cursor = _FakeCursor([{"id": TENANT_ID}])
        with mock.patch.object(
            sr, "connect", return_value=_FakeConnection(cursor)
        ), self.assertRaises(sr.SettingError):
            sr.apply(
                settings(),
                {"sql.max_rows": 100, "sql.timeout_ms": 999_999_999},
            )
        # Validation runs before any statement, so no insert was attempted.
        self.assertEqual(len(cursor.statements), 0)


class ResolutionTests(unittest.TestCase):
    """Tenant override, then global, then default — and the source is reported."""

    def rows(self, values):
        return [
            sr.StoredSetting(key=key, scope=scope, value=value, version=1)
            for (scope, key), value in values.items()
        ]

    def test_an_unset_key_resolves_to_its_default(self) -> None:
        resolution = sr.resolve("sql.max_rows", [])
        self.assertEqual(resolution.source, "default")
        self.assertEqual(resolution.value, sr.BY_KEY["sql.max_rows"].default)
        self.assertEqual(resolution.version, 0)

    def test_a_global_row_wins_over_the_default(self) -> None:
        resolution = sr.resolve(
            "sql.max_rows", self.rows({("global", "sql.max_rows"): 5_000})
        )
        self.assertEqual((resolution.value, resolution.source), (5_000, "global"))

    def test_a_tenant_row_wins_over_the_global_row(self) -> None:
        resolution = sr.resolve(
            "sql.max_rows",
            self.rows(
                {
                    ("global", "sql.max_rows"): 5_000,
                    ("tenant", "sql.max_rows"): 2_000,
                }
            ),
        )
        self.assertEqual((resolution.value, resolution.source), (2_000, "tenant"))

    def test_a_tenant_row_is_ignored_for_a_global_only_setting(self) -> None:
        # usage.alert_email is global-only, so a stray tenant row must not win.
        resolution = sr.resolve(
            "usage.alert_email",
            self.rows(
                {
                    ("global", "usage.alert_email"): "ops@example.com",
                    ("tenant", "usage.alert_email"): "tenant@example.com",
                }
            ),
        )
        self.assertEqual(resolution.value, "ops@example.com")
        self.assertEqual(resolution.source, "global")


class ApplyTests(unittest.TestCase):
    """Writes are version-checked, audited, and invalidate the cache."""

    def apply(self, rows, **kwargs):
        # Global scope never looks a tenant up, so the script starts at the
        # existing-row check.
        cursor = _FakeCursor(rows)
        with mock.patch.object(sr, "connect", return_value=_FakeConnection(cursor)):
            result = sr.apply(settings(), **kwargs)
        return result, cursor

    def update(self, **overrides):
        body = {"sql.max_rows": 5_000}
        body.update(overrides)
        return body

    def test_a_write_bumps_the_version_and_audits_before_and_after(self) -> None:
        result, cursor = self.apply(
            [None, {"key": "sql.max_rows", "scope": "global", "value": 5_000,
                    "version": 1}],
            updates=self.update(),
            reason="tighter cap",
        )

        self.assertEqual(result[0].version, 1)
        audited = [sql for sql in cursor.statements if "INSERT INTO ai_setting_audit" in sql]
        self.assertEqual(len(audited), 1)
        audit_params = cursor.params[cursor.statements.index(audited[0])]
        # (tenant, scope, key, version, before, after, actor, reason)
        self.assertEqual(audit_params[7], "tighter cap")

    def test_a_second_write_records_the_previous_value(self) -> None:
        _, cursor = self.apply(
            [{"version": 1, "value": "5000"}, {"key": "sql.max_rows"}],
            updates=self.update(),
        )
        audit_params = cursor.params[
            next(
                index
                for index, sql in enumerate(cursor.statements)
                if "INSERT INTO ai_setting_audit" in sql
            )
        ]
        self.assertEqual(json.loads(audit_params[4]), 5000)  # before
        self.assertEqual(json.loads(audit_params[5]), 5000)  # after
        self.assertEqual(audit_params[3], 2)  # version

    def test_a_stale_version_is_a_conflict(self) -> None:
        with self.assertRaises(sr.SettingError) as caught:
            self.apply(
                [{"version": 7, "value": '"1000"'}],
                updates=self.update(),
                expected_version=3,
            )
        self.assertEqual(caught.exception.code, "version_conflict")

    def test_a_matching_version_is_accepted(self) -> None:
        result, _ = self.apply(
            [{"version": 3, "value": '"1000"'}, {"key": "sql.max_rows"}],
            updates=self.update(),
            expected_version=3,
        )
        self.assertEqual(result[0].version, 4)

    def test_the_cache_is_invalidated_after_a_write(self) -> None:
        sr._CACHE.stamp = 99
        self.apply([None, {"key": "sql.max_rows"}], updates=self.update())
        self.assertEqual(sr._CACHE.stamp, -1)

    def test_an_unknown_scope_is_refused(self) -> None:
        with self.assertRaises(sr.SettingError) as caught:
            self.apply([], updates=self.update(), scope="platform")
        self.assertEqual(caught.exception.code, "invalid_scope")


class RollbackTests(unittest.TestCase):
    def test_a_rollback_restores_the_previous_value_as_a_new_version(self) -> None:
        cursor = _FakeCursor(
            [
                {"id": TENANT_ID},  # _tenant_id resolves the caller's tenant
                {"id": AUDIT_ID, "tenant_id": None, "scope": "global",
                 "key": "sql.max_rows", "before_value": '"10000"'},
                None,
                {"key": "sql.max_rows"},
            ]
        )
        with mock.patch.object(sr, "connect", return_value=_FakeConnection(cursor)):
            resolution = sr.rollback(settings(), AUDIT_ID, reason="too tight")

        # The stored JSON is the string "10000"; the integer setting coerces it.
        self.assertEqual(resolution.value, 10_000)
        audit_sql = [sql for sql in cursor.statements if "ai_setting_audit" in sql]
        # One SELECT of the audit row, then the INSERT of the rollback itself.
        self.assertEqual(len(audit_sql), 2)

    def test_an_unknown_change_is_not_found(self) -> None:
        cursor = _FakeCursor([{"id": TENANT_ID}, None])
        with mock.patch.object(
            sr, "connect", return_value=_FakeConnection(cursor)
        ), self.assertRaises(sr.SettingError) as caught:
            sr.rollback(settings(), AUDIT_ID)
        self.assertEqual(caught.exception.code, "audit_not_found")

    def test_rolling_back_a_creation_has_nothing_to_restore(self) -> None:
        cursor = _FakeCursor(
            [{"id": TENANT_ID},
             {"id": AUDIT_ID, "scope": "global", "key": "sql.max_rows",
              "before_value": None}]
        )
        with mock.patch.object(
            sr, "connect", return_value=_FakeConnection(cursor)
        ), self.assertRaises(sr.SettingError) as caught:
            sr.rollback(settings(), AUDIT_ID)
        self.assertEqual(caught.exception.code, "nothing_to_restore")


class FeatureTests(unittest.TestCase):
    def test_an_unknown_feature_is_off(self) -> None:
        self.assertFalse(sr.feature_enabled(settings(), "telepathy"))

    def test_a_known_feature_defaults_to_on(self) -> None:
        cursor = _FakeCursor([{"id": TENANT_ID}, {"stamp": 0}, []])
        with mock.patch.object(sr, "connect", return_value=_FakeConnection(cursor)):
            sr._CACHE.stamp = -9
            self.assertTrue(sr.feature_enabled(settings(), "chat"))


if __name__ == "__main__":
    unittest.main()
