"""The settings a module actually reads, and what happens when they disagree.

A registry that nothing consumes is a form, not configuration. These tests hold
the three properties that make consumption safe:

* reading configuration never fails a request — an unreachable table falls back
  to the documented defaults, which is the state of a fresh deployment;
* a ceiling only ever tightens what a module asks for, so opening it never
  silently lengthens every answer;
* a stored override wins over the deployment's environment default, and its
  absence leaves existing configuration untouched.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from xp_prompt import chat, repair, schema_slice, sql_candidate
from xp_prompt import settings_registry as sr
from xp_prompt import patterns, sql_execution, sql_guard
from xp_prompt.config import Settings

ROOT_SECRET = "unit-test-root-secret-value"


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


def live_map(**overrides: object) -> dict[str, sr.Resolution]:
    """Every setting at its default, with the named keys replaced."""
    values = {
        setting.key: sr.Resolution(setting.key, setting.default, "default", 0)
        for setting in sr.REGISTRY
    }
    for key, value in overrides.items():
        dotted = key.replace("__", ".")
        values[dotted] = sr.Resolution(dotted, value, "tenant", 1)
    return values


def message(role: str, content: str, identifier: str) -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, id=identifier)


class LiveTests(unittest.TestCase):
    """Reading configuration is best-effort; a broken table is not a 500."""

    def test_an_unreadable_table_falls_back_to_the_documented_defaults(self) -> None:
        with mock.patch.object(sr, "load", side_effect=RuntimeError("db is down")):
            values = sr.live(settings())
        self.assertEqual(
            {key: resolution.value for key, resolution in values.items()},
            {setting.key: setting.default for setting in sr.REGISTRY},
        )
        self.assertTrue(all(r.source == "default" for r in values.values()))

    def test_a_failed_read_does_not_invent_an_override(self) -> None:
        with mock.patch.object(sr, "load", side_effect=RuntimeError("db is down")):
            values = sr.live(settings())
        # A default is not an override: modules keep their own configuration.
        self.assertIsNone(sr.override(values, "repair.max_attempts"))

    def test_an_override_is_reported_when_one_exists(self) -> None:
        values = live_map(repair__max_attempts=1)
        self.assertEqual(sr.override(values, "repair.max_attempts"), 1)

    def test_the_source_survives_a_real_resolution(self) -> None:
        rows = [sr.StoredSetting("generation.max_output_tokens", "global", 4096, 3)]
        key = "generation.max_output_tokens"
        self.assertEqual(sr.resolve(key, rows).source, "global")
        self.assertEqual(sr.resolve(key, []).source, "default")


class ChatGenerationTests(unittest.TestCase):
    """The chat turn reads generation settings instead of its own constants."""

    def test_without_settings_the_turn_is_unchanged(self) -> None:
        self.assertEqual(chat._max_output_tokens(None), chat.CHAT_MAX_OUTPUT_TOKENS)
        self.assertEqual(chat._temperature(None), chat.CHAT_TEMPERATURE)
        self.assertEqual(chat._thinking_budget(None), 0)

    def test_the_registry_default_reproduces_the_module_constant(self) -> None:
        self.assertEqual(
            sr.BY_KEY["generation.temperature_basis_points"].default,
            int(chat.CHAT_TEMPERATURE * 100),
        )

    def test_a_lower_ceiling_tightens_the_answer(self) -> None:
        values = live_map(generation__max_output_tokens=256)
        self.assertEqual(chat._max_output_tokens(values), 256)

    def test_opening_the_ceiling_does_not_lengthen_a_chat_answer(self) -> None:
        values = live_map(generation__max_output_tokens=32_768)
        self.assertEqual(chat._max_output_tokens(values), chat.CHAT_MAX_OUTPUT_TOKENS)

    def test_temperature_is_read_in_hundredths(self) -> None:
        warm = live_map(generation__temperature_basis_points=150)
        frozen = live_map(generation__temperature_basis_points=0)
        self.assertAlmostEqual(chat._temperature(warm), 1.5)
        self.assertAlmostEqual(chat._temperature(frozen), 0.0)

    def test_the_thinking_budget_is_carried(self) -> None:
        values = live_map(generation__thinking_budget_tokens=512)
        self.assertEqual(chat._thinking_budget(values), 512)

    def test_the_request_carries_the_resolved_values(self) -> None:
        history = [
            message("user", "how many orders yesterday?", "m1"),
            message("assistant", "asking for a date range instead", "m2"),
            message("user", "last 24 hours", "m3"),
        ]
        values = live_map(
            generation__max_output_tokens=300,
            generation__temperature_basis_points=75,
            generation__thinking_budget_tokens=128,
        )
        request = chat._build_request(history, "m3", live=values)
        self.assertEqual(request.max_output_tokens, 300)
        self.assertAlmostEqual(request.temperature, 0.75)
        self.assertEqual(request.reasoning_budget_tokens, 128)

    def test_the_default_request_matches_the_old_behaviour(self) -> None:
        history = [message("user", "hello", "m1")]
        request = chat._build_request(history, "m1")
        self.assertEqual(request.max_output_tokens, chat.CHAT_MAX_OUTPUT_TOKENS)
        self.assertAlmostEqual(request.temperature, chat.CHAT_TEMPERATURE)

    def test_a_switched_off_feature_refuses_the_turn(self) -> None:
        with mock.patch.object(
            chat.settings_registry, "live", return_value=live_map(feature__chat=False)
        ):
            with self.assertRaises(chat.ChatError) as caught:
                chat.run_turn(
                    settings(),
                    conversation_id="c1",
                    tenant_key="default",
                    superset_user_id=1,
                    content="hello",
                )
        self.assertEqual(caught.exception.code, "feature_disabled")


class SqlGenerationTests(unittest.TestCase):
    """SQL generation keeps its own temperature and obeys the same ceiling."""

    def test_without_settings_the_candidate_is_unchanged(self) -> None:
        self.assertEqual(
            sql_candidate._max_output_tokens(None), sql_candidate.CANDIDATE_MAX_TOKENS
        )

    def test_the_ceiling_tightens_the_candidate(self) -> None:
        values = live_map(generation__max_output_tokens=512)
        self.assertEqual(sql_candidate._max_output_tokens(values), 512)

    def test_sql_generation_never_inherits_the_chat_temperature(self) -> None:
        # A repaired statement has to be reproducible before it can be validated.
        self.assertEqual(sql_candidate.CANDIDATE_TEMPERATURE, 0.0)

    def test_a_switched_off_feature_refuses_generation(self) -> None:
        with mock.patch.object(
            sql_candidate.settings_registry,
            "live",
            return_value=live_map(feature__sql=False),
        ):
            with self.assertRaises(sql_candidate.SqlCandidateError) as caught:
                sql_candidate.generate(
                    settings(),
                    question="how many orders?",
                    schema=mock.MagicMock(),
                    dialect="postgres",
                )
        self.assertEqual(caught.exception.code, "feature_disabled")


class RepairBudgetTests(unittest.TestCase):
    """An override wins; its absence leaves the environment alone."""

    def test_without_an_override_the_environment_budget_stands(self) -> None:
        configured = settings()
        budget = repair._repair_budget(configured, live_map())
        self.assertEqual(budget.max_attempts, configured.repair_max_attempts)

    def test_an_override_replaces_only_the_attempt_limit(self) -> None:
        configured = settings()
        baseline = repair._repair_budget(configured, live_map())
        budget = repair._repair_budget(configured, live_map(repair__max_attempts=1))
        self.assertEqual(budget.max_attempts, 1)
        self.assertEqual(budget.max_elapsed_ms, baseline.max_elapsed_ms)
        self.assertEqual(budget.max_tokens, baseline.max_tokens)

    def test_zero_attempts_disables_repair(self) -> None:
        budget = repair._repair_budget(settings(), live_map(repair__max_attempts=0))
        self.assertEqual(budget.max_attempts, 0)

    def test_promotion_off_outranks_any_collectable_evidence(self) -> None:
        # ``_settled_state`` needs ``len(sources) >= threshold`` and the evidence
        # list is capped, so this threshold can never be reached.
        self.assertGreater(repair.PROMOTION_OFF, patterns.MAX_EVIDENCE)
        settled = patterns._settled_state(
            "validated", 9_999, 0, ["a", "b", "c"], repair.PROMOTION_OFF
        )
        self.assertEqual(settled, "validated")

    def test_the_registry_default_matches_the_candidate_budget(self) -> None:
        self.assertEqual(
            sr.BY_KEY["generation.max_output_tokens"].default,
            sql_candidate.CANDIDATE_MAX_TOKENS,
        )


def slice_(datasets: list[dict] | None = None) -> schema_slice.SchemaSlice:
    return schema_slice.SchemaSlice.from_payload(
        {
            "datasets": datasets
            if datasets is not None
            else [
                {
                    "name": "mdl_user",
                    "schema": "public",
                    "database": "lms",
                    "dialect": "postgres",
                    "columns": [{"name": "id", "type": "int"}],
                }
            ]
        }
    )


class SqlLimitsTests(unittest.TestCase):
    """The four safeguards are read from the registry and held at their ceilings."""

    def limits(self, **overrides: object) -> sr.SqlLimits:
        with mock.patch.object(sr, "live", return_value=live_map(**overrides)):
            return sr.sql_limits(settings())

    def test_the_documented_defaults_apply(self) -> None:
        limits = self.limits()
        self.assertEqual(limits.max_rows, sr.BY_KEY["sql.max_rows"].default)
        self.assertEqual(limits.timeout_ms, sr.BY_KEY["sql.timeout_ms"].default)
        self.assertEqual(limits.max_concurrent, sr.BY_KEY["sql.max_concurrent"].default)
        self.assertFalse(limits.allow_metadata_schemas)

    def test_a_row_edited_above_the_ceiling_is_held_down(self) -> None:
        # The write path refuses this value; a row edited directly must not
        # raise a safeguard either, or the ceiling is only a default.
        ceiling = int(sr.BY_KEY["sql.max_rows"].maximum)
        limits = self.limits(sql__max_rows=ceiling * 10)
        self.assertEqual(limits.max_rows, ceiling)

    def test_a_row_edited_below_the_floor_is_raised(self) -> None:
        floor = int(sr.BY_KEY["sql.timeout_ms"].minimum)
        limits = self.limits(sql__timeout_ms=1)
        self.assertEqual(limits.timeout_ms, floor)

    def test_every_safeguard_is_marked_as_a_ceiling(self) -> None:
        for key in (
            "sql.max_rows",
            "sql.timeout_ms",
            "sql.max_concurrent",
            "sql.allow_metadata_schemas",
        ):
            self.assertTrue(sr.BY_KEY[key].ceiling, key)

    def test_the_public_shape_names_all_four(self) -> None:
        body = self.limits().to_public_dict()
        self.assertEqual(
            sorted(body),
            [
                "allow_metadata_schemas",
                "max_concurrent",
                "max_rows",
                "timeout_ms",
            ],
        )


class SqlGuardCeilingTests(unittest.TestCase):
    """A request may narrow the safeguards; it may never widen them."""

    def limits(self, **overrides: object) -> sr.SqlLimits:
        with mock.patch.object(sr, "live", return_value=live_map(**overrides)):
            return sr.sql_limits(settings())

    def test_metadata_schemas_are_refused_by_default(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError) as caught:
            sql_guard.check(
                "SELECT count(*) FROM pg_catalog.pg_class", dialect="postgres"
            )
        self.assertEqual(caught.exception.code, "system_schema")

    def test_the_operator_can_open_metadata_schemas(self) -> None:
        guarded = sql_guard.check(
            "SELECT count(*) FROM pg_catalog.pg_class",
            dialect="postgres",
            allow_metadata_schemas=True,
        )
        self.assertEqual(guarded.limit, sql_guard.MAX_ROWS)

    def test_a_request_cannot_raise_the_row_cap(self) -> None:
        result = sql_execution.validate(
            "SELECT id FROM mdl_user",
            schema=slice_(),
            database="lms",
            max_rows=50_000,
            limits=self.limits(sql__max_rows=1_000),
        )
        self.assertEqual(result["limit"], 1_000)

    def test_a_request_may_narrow_the_row_cap(self) -> None:
        result = sql_execution.validate(
            "SELECT id FROM mdl_user",
            schema=slice_(),
            database="lms",
            max_rows=10,
            limits=self.limits(sql__max_rows=1_000),
        )
        self.assertEqual(result["limit"], 10)

    def test_the_executor_is_told_the_rest_of_the_safeguards(self) -> None:
        limits = self.limits(sql__timeout_ms=5_000, sql__max_concurrent=2)
        result = sql_execution.validate(
            "SELECT id FROM mdl_user",
            schema=slice_(),
            database="lms",
            limits=limits,
        )
        self.assertEqual(result["limits"]["timeout_ms"], 5_000)
        self.assertEqual(result["limits"]["max_concurrent"], 2)

    def test_without_limits_the_caller_decides(self) -> None:
        result = sql_execution.validate(
            "SELECT id FROM mdl_user", schema=slice_(), database="lms", max_rows=5_000
        )
        self.assertEqual(result["limit"], 5_000)
        self.assertIsNone(result["limits"])


if __name__ == "__main__":
    unittest.main()
