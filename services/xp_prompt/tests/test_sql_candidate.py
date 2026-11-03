"""Tests for candidate SQL generation (P4-F04)."""

from __future__ import annotations

import json
import unittest
from unittest import mock

from xp_prompt import schema_slice, sql_candidate
from xp_prompt.config import Settings
from xp_prompt.providers.types import (
    AttemptSummary,
    ProviderError,
    ProviderResponse,
    ProviderRoutingError,
    RoutedResponse,
    TokenUsage,
)

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


def slice_(datasets=None) -> schema_slice.SchemaSlice:
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


def routed(content: str) -> RoutedResponse:
    return RoutedResponse(
        response=ProviderResponse(
            provider="gemini",
            model="gemini-2.5-flash",
            content=content,
            finish_reason="stop",
            request_id="req-1",
            usage=TokenUsage(10, 0, 5, 0, 15, "reported"),
        ),
        attempts=(
            AttemptSummary(
                provider="gemini",
                model="gemini-2.5-flash",
                outcome="succeeded",
                attempt=1,
                correlation_id="corr-1",
                operation="sql.candidate",
            ),
        ),
    )


def document(sql: str = "select id from mdl_user", **overrides) -> str:
    body = {
        "sql": sql,
        "rationale": "Counts users.",
        "assumptions": ["The table is current."],
    }
    body.update(overrides)
    return json.dumps(body)


class BuildRequestTest(unittest.TestCase):
    def test_prompt_contains_only_authorized_tables(self) -> None:
        request = sql_candidate.build_request(
            "how many users", slice_(), dialect="postgres", max_rows=100
        )

        user_message = request.messages[1].content
        self.assertIn("public.mdl_user", user_message)
        self.assertIn("id int", user_message)
        self.assertNotIn("mdl_course", user_message)

    def test_prompt_states_the_dialect_and_the_cap(self) -> None:
        request = sql_candidate.build_request(
            "how many users", slice_(), dialect="mysql", max_rows=42
        )

        system = request.messages[0].content
        self.assertIn("mysql", system)
        self.assertIn("42", system)

    def test_request_asks_for_validated_json(self) -> None:
        request = sql_candidate.build_request(
            "how many users", slice_(), dialect="postgres", max_rows=100
        )

        self.assertTrue(request.json_output)
        self.assertEqual(request.json_required_keys, sql_candidate.REQUIRED_KEYS)
        self.assertEqual(request.operation, "sql.candidate")
        self.assertEqual(request.temperature, 0.0)

    def test_empty_question_is_rejected(self) -> None:
        with self.assertRaises(sql_candidate.SqlCandidateError) as raised:
            sql_candidate.build_request(
                "   ", slice_(), dialect="postgres", max_rows=100
            )

        self.assertEqual(raised.exception.code, "empty_question")

    def test_an_empty_slice_is_rejected(self) -> None:
        with self.assertRaises(sql_candidate.SqlCandidateError) as raised:
            sql_candidate.build_request(
                "how many users", slice_([]), dialect="postgres", max_rows=100
            )

        self.assertEqual(raised.exception.code, "no_schema")


class DialectTest(unittest.TestCase):
    def test_explicit_dialect_is_normalised(self) -> None:
        request = sql_candidate.build_request(
            "q", slice_(), dialect="POSTGRES", max_rows=10
        )
        self.assertIn("postgres", request.messages[0].content)

    def test_database_selects_the_dialect(self) -> None:
        schema = slice_(
            [
                {
                    "name": "a",
                    "database": "lms",
                    "dialect": "postgres",
                    "columns": [{"name": "id"}],
                },
                {
                    "name": "b",
                    "database": "blog",
                    "dialect": "mysql",
                    "columns": [{"name": "id"}],
                },
            ]
        )

        self.assertEqual(
            sql_candidate._resolve_dialect(schema, database="blog", dialect=None),
            "mysql",
        )

    def test_several_dialects_without_a_target_is_ambiguous(self) -> None:
        schema = slice_(
            [
                {"name": "a", "database": "lms", "dialect": "postgres"},
                {"name": "b", "database": "blog", "dialect": "mysql"},
            ]
        )

        with self.assertRaises(sql_candidate.SqlCandidateError) as raised:
            sql_candidate._resolve_dialect(schema, database=None, dialect=None)

        self.assertEqual(raised.exception.code, "dialect_ambiguous")

    def test_unsupported_dialect_is_refused(self) -> None:
        with self.assertRaises(sql_candidate.SqlCandidateError) as raised:
            sql_candidate._resolve_dialect(slice_(), database=None, dialect="oracle")

        self.assertEqual(raised.exception.code, "unsupported_dialect")


class ParseCandidateTest(unittest.TestCase):
    def test_valid_document_is_accepted(self) -> None:
        sql, rationale, assumptions = sql_candidate._parse_candidate(document())

        self.assertEqual(sql, "select id from mdl_user")
        self.assertEqual(rationale, "Counts users.")
        self.assertEqual(assumptions, ("The table is current.",))

    def test_fenced_json_is_accepted(self) -> None:
        fenced = "```json\n" + document() + "\n```"

        sql, _, _ = sql_candidate._parse_candidate(fenced)
        self.assertEqual(sql, "select id from mdl_user")

    def test_missing_key_is_rejected(self) -> None:
        with self.assertRaises(sql_candidate.SqlCandidateError) as raised:
            sql_candidate._parse_candidate(json.dumps({"sql": "select 1"}))

        self.assertEqual(raised.exception.code, "invalid_response")

    def test_prose_is_rejected(self) -> None:
        with self.assertRaises(sql_candidate.SqlCandidateError):
            sql_candidate._parse_candidate("SELECT * FROM mdl_user")

    def test_non_string_sql_is_rejected(self) -> None:
        with self.assertRaises(sql_candidate.SqlCandidateError):
            sql_candidate._parse_candidate(document(sql=["select 1"]))

    def test_assumptions_are_bounded(self) -> None:
        many = [f"assumption {index}" for index in range(20)]
        _, _, assumptions = sql_candidate._parse_candidate(document(assumptions=many))

        self.assertEqual(len(assumptions), sql_candidate.MAX_ASSUMPTIONS)
        self.assertLessEqual(
            len(assumptions[0]), sql_candidate.MAX_ASSUMPTION_CHARS
        )

    def test_blank_assumptions_are_dropped(self) -> None:
        _, _, assumptions = sql_candidate._parse_candidate(
            document(assumptions=["  ", "real"])
        )

        self.assertEqual(assumptions, ("real",))


class GenerateTest(unittest.TestCase):
    def run_with(self, content: str, *, schema=None, dialect="postgres"):
        router = mock.Mock()
        router.generate.return_value = routed(content)
        with mock.patch.object(
            sql_candidate.registry, "resolve_router", return_value=router
        ):
            return sql_candidate.generate(
                settings(),
                question="how many users",
                schema=schema or slice_(),
                dialect=dialect,
            )

    def test_valid_candidate_is_guarded_and_gets_blocks(self) -> None:
        candidate = self.run_with(document())

        self.assertIn("LIMIT", candidate.sql.upper())
        self.assertEqual(candidate.tables, ("mdl_user",))
        self.assertEqual(candidate.correlation_id, "corr-1")
        self.assertEqual(
            [block["type"] for block in candidate.blocks],
            ["sql_preview", "assumptions", "warnings"],
        )
        self.assertEqual(candidate.blocks[0]["dialect"], "postgres")

    def test_no_warning_is_silent_about_execution(self) -> None:
        candidate = self.run_with(document())

        warnings = [b for b in candidate.blocks if b["type"] == "warnings"]
        self.assertIn("Nothing has been executed", warnings[0]["items"][0])

    def test_unauthorized_table_is_blocked(self) -> None:
        with self.assertRaises(sql_candidate.SqlCandidateError) as raised:
            self.run_with(document("select * from mdl_secret"))

        self.assertEqual(raised.exception.code, "sql_blocked")
        self.assertEqual(raised.exception.guard_code, "unknown_table")

    def test_write_statement_is_blocked(self) -> None:
        with self.assertRaises(sql_candidate.SqlCandidateError) as raised:
            self.run_with(document("delete from mdl_user"))

        self.assertEqual(raised.exception.guard_code, "not_read_only")

    def test_metadata_schema_is_blocked(self) -> None:
        with self.assertRaises(sql_candidate.SqlCandidateError) as raised:
            self.run_with(document("select * from information_schema.tables"))

        self.assertEqual(raised.exception.guard_code, "system_schema")

    def test_the_block_message_does_not_echo_the_sql(self) -> None:
        try:
            self.run_with(document("select * from mdl_secret"))
        except sql_candidate.SqlCandidateError as error:
            self.assertNotIn("mdl_secret", str(error))

    def test_provider_failure_is_reported(self) -> None:
        router = mock.Mock()
        router.generate.side_effect = ProviderRoutingError(
            cause=ProviderError(
                provider="gemini", code="http_503", message="down", retryable=True
            ),
            attempts=(),
            correlation_id="corr-2",
        )
        with mock.patch.object(
            sql_candidate.registry, "resolve_router", return_value=router
        ):
            with self.assertRaises(sql_candidate.SqlCandidateError) as raised:
                sql_candidate.generate(
                    settings(), question="q", schema=slice_(), dialect="postgres"
                )

        self.assertEqual(raised.exception.code, "provider_error")

    def test_structured_output_failure_is_an_upstream_shape_error(self) -> None:
        router = mock.Mock()
        router.generate.side_effect = ProviderRoutingError(
            cause=ProviderError(
                provider="gemini",
                code="structured_output_invalid",
                message="structured output is not valid JSON (Unterminated string)",
                retryable=False,
            ),
            attempts=(),
            correlation_id="corr-3",
        )
        with mock.patch.object(
            sql_candidate.registry, "resolve_router", return_value=router
        ):
            with self.assertRaises(sql_candidate.SqlCandidateError) as raised:
                sql_candidate.generate(
                    settings(), question="q", schema=slice_(), dialect="postgres"
                )

        self.assertEqual(raised.exception.code, "invalid_response")
        self.assertEqual(raised.exception.provider_code, "structured_output_invalid")

    def test_missing_provider_is_reported(self) -> None:
        with mock.patch.object(
            sql_candidate.registry,
            "resolve_router",
            side_effect=sql_candidate.registry.ProviderConfigError("no provider"),
        ):
            with self.assertRaises(sql_candidate.SqlCandidateError) as raised:
                sql_candidate.generate(
                    settings(), question="q", schema=slice_(), dialect="postgres"
                )

        self.assertEqual(raised.exception.code, "no_provider")

    def test_public_dict_exposes_the_contract(self) -> None:
        payload = self.run_with(document()).to_public_dict()

        self.assertEqual(
            sorted(payload),
            [
                "assumptions",
                "blocks",
                "correlation_id",
                "dialect",
                "limit",
                "model",
                "provider",
                "rationale",
                "schema_fingerprint",
                "sql",
                "tables",
            ],
        )


if __name__ == "__main__":
    unittest.main()
