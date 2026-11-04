"""Tests for the repair decision (P5-F04/F05/F06/F07/F08)."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from unittest import mock

from xp_prompt import patterns, repair, schema_slice
from xp_prompt.config import Settings
from xp_prompt.providers.types import (
    AttemptSummary,
    ProviderResponse,
    RoutedResponse,
    TokenUsage,
)

ROOT_SECRET = "unit-test-root-secret-value"


def settings(**overrides) -> Settings:
    base = Settings.from_env(
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
            "XP_PROMPT_CANDIDATE_CACHE": "false",
        }
    )
    return replace(base, **overrides)


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
                    "columns": [
                        {"name": "id", "type": "int"},
                        {"name": "firstname", "type": "text"},
                    ],
                }
            ]
        }
    )


def routed(sql: str) -> RoutedResponse:
    content = json.dumps(
        {"sql": sql, "rationale": "Corrected.", "assumptions": ["Assumed current."]}
    )
    return RoutedResponse(
        response=ProviderResponse(
            provider="gemini",
            model="gemini-2.5-flash",
            content=content,
            finish_reason="stop",
            request_id="req-2",
            usage=TokenUsage(10, 0, 5, 0, 15, "reported"),
        ),
        attempts=(
            AttemptSummary(
                provider="gemini",
                model="gemini-2.5-flash",
                outcome="succeeded",
                attempt=1,
                correlation_id="corr-2",
                operation="sql.candidate",
            ),
        ),
    )


GOOD_SQL = "select id from public.mdl_user limit 10"


class RepairNoteTest(unittest.TestCase):
    def test_the_note_carries_the_kind_and_the_shape_not_the_data(self) -> None:
        from xp_prompt import failures

        event = failures.classify(
            stage="execute",
            message="ProgrammingError: column 'secret_col' does not exist",
        )
        note = repair.repair_note(event, "select secret_col from t")
        self.assertIn("unknown_identifier", note)
        self.assertIn("select secret_col from t", note)
        self.assertIn("Change only what", note)

    def test_the_note_is_bounded(self) -> None:
        from xp_prompt import failures

        event = failures.classify(stage="execute", message="boom")
        note = repair.repair_note(event, "select " + "x" * 5_000)
        self.assertLessEqual(len(note), repair.MAX_FAILED_SQL_CHARS + 400)


class TerminalRefusalTest(unittest.TestCase):
    def test_a_policy_refusal_never_reaches_the_pattern_store(self) -> None:
        with mock.patch.object(patterns, "find") as find, mock.patch.object(
            repair.sql_candidate, "generate"
        ) as generate:
            attempt = repair.propose(
                settings(),
                question="how many users",
                schema=slice_(),
                tenant_key="default",
                sqlstate="42501",
                message="permission denied for table mdl_user",
            )
        self.assertFalse(attempt.allowed)
        self.assertEqual(attempt.reason, "terminal_policy")
        find.assert_not_called()
        generate.assert_not_called()

    def test_an_exhausted_budget_refuses_before_any_generation(self) -> None:
        with mock.patch.object(repair.sql_candidate, "generate") as generate:
            attempt = repair.propose(
                settings(),
                question="how many users",
                schema=slice_(),
                tenant_key="default",
                message="relation does not exist",
                attempts=5,
            )
        self.assertFalse(attempt.allowed)
        self.assertEqual(attempt.reason, "attempts_exhausted")
        generate.assert_not_called()

    def test_the_elapsed_budget_is_honoured(self) -> None:
        attempt = repair.propose(
            settings(),
            question="how many users",
            schema=slice_(),
            tenant_key="default",
            message="relation does not exist",
            elapsed_ms=30_000,
        )
        self.assertFalse(attempt.allowed)
        self.assertEqual(attempt.reason, "deadline_exceeded")


class PatternReuseTest(unittest.TestCase):
    def test_a_promoted_pattern_is_used_before_generating(self) -> None:
        stored = patterns.Pattern(
            id="p1",
            state="promoted",
            version=2,
            dialect="postgres",
            kind="unknown_identifier",
            failure_fingerprint="f" * 64,
            schema_fingerprint=slice_().fingerprint,
            intent="query",
            correction_sha256="c" * 64,
            successes=4,
            confidence=0.83,
            validator_version=patterns.VALIDATOR_VERSION,
            policy_version=patterns.POLICY_VERSION,
            sql=GOOD_SQL,
        )
        with mock.patch.object(patterns, "find", return_value=[stored]), mock.patch.object(
            repair.sql_candidate, "generate"
        ) as generate:
            attempt = repair.propose(
                settings(),
                question="how many users",
                schema=slice_(),
                tenant_key="default",
                message="relation does not exist",
            )
        self.assertTrue(attempt.allowed)
        self.assertEqual(attempt.source, repair.PATTERN)
        self.assertEqual(attempt.pattern_id, "p1")
        self.assertEqual(attempt.sql.upper(), GOOD_SQL.upper())
        generate.assert_not_called()

    def test_a_pattern_that_no_longer_validates_is_quarantined(self) -> None:
        stored = patterns.Pattern(
            id="p1",
            state="promoted",
            version=1,
            dialect="postgres",
            kind="unknown_identifier",
            failure_fingerprint="f" * 64,
            schema_fingerprint=slice_().fingerprint,
            intent="query",
            correction_sha256="c" * 64,
            successes=4,
            confidence=0.83,
            validator_version=patterns.VALIDATOR_VERSION,
            policy_version=patterns.POLICY_VERSION,
            sql="delete from public.mdl_user",
        )
        with mock.patch.object(patterns, "find", return_value=[stored]), mock.patch.object(
            patterns, "set_state"
        ) as set_state, mock.patch.object(
            repair.sql_candidate, "generate"
        ) as generate, mock.patch.object(repair.failures, "record_attempt"):
            attempt = repair.propose(
                settings(),
                question="how many users",
                schema=slice_(),
                tenant_key="default",
                message="relation does not exist",
            )
        set_state.assert_called_once()
        self.assertEqual(set_state.call_args.kwargs["action"], "quarantine")
        # Falling through to a generated correction is the point of quarantining.
        generate.assert_called_once()
        self.assertIn("pattern_rejected", attempt.explanation)


class GenerationTest(unittest.TestCase):
    def test_a_valid_correction_is_proposed_and_recorded(self) -> None:
        with mock.patch.object(patterns, "find", return_value=[]), mock.patch.object(
            repair.sql_candidate, "generate"
        ) as generate, mock.patch.object(repair.failures, "record_attempt"):
            generate.return_value = mock.Mock(
                sql=GOOD_SQL, dialect="postgres", tables=("public.mdl_user",)
            )
            attempt = repair.propose(
                settings(),
                question="how many users",
                schema=slice_(),
                tenant_key="default",
                message="relation does not exist",
            )
        self.assertTrue(attempt.allowed)
        self.assertEqual(attempt.source, repair.GENERATED)
        self.assertEqual(attempt.sql.upper(), GOOD_SQL.upper())
        self.assertTrue(attempt.fix_fingerprint)
        # The repair note, not a rewritten question, is what the provider is told.
        self.assertTrue(generate.call_args.kwargs["repair_note"])
        self.assertTrue(generate.call_args.kwargs["bypass_cache"])

    def test_a_previously_failed_fix_is_suppressed(self) -> None:
        existing = "select id from public.mdl_user limit 10"
        already_failed = {repair.failures.fix_fingerprint(existing)}
        with mock.patch.object(patterns, "find", return_value=[]), mock.patch.object(
            repair.failures, "failed_fixes", return_value=already_failed
        ), mock.patch.object(
            repair.sql_candidate, "generate"
        ) as generate, mock.patch.object(
            repair.failures, "record_attempt"
        ) as record:
            generate.return_value = mock.Mock(
                sql=existing, dialect="postgres", tables=()
            )
            attempt = repair.propose(
                settings(),
                question="how many users",
                schema=slice_(),
                tenant_key="default",
                message="relation does not exist",
            )
        self.assertFalse(attempt.allowed)
        self.assertEqual(attempt.reason, "duplicate_fix")
        self.assertEqual(record.call_args.kwargs["outcome"], "suppressed")

    def test_a_correction_that_breaks_the_guard_is_refused(self) -> None:
        with mock.patch.object(patterns, "find", return_value=[]), mock.patch.object(
            repair.sql_candidate, "generate"
        ) as generate, mock.patch.object(repair.failures, "record_attempt"):
            generate.return_value = mock.Mock(
                sql="select id from mdl_course limit 10", dialect="postgres", tables=()
            )
            attempt = repair.propose(
                settings(),
                question="how many users",
                schema=slice_(),
                tenant_key="default",
                message="relation does not exist",
            )
        self.assertFalse(attempt.allowed)
        self.assertEqual(attempt.reason, "invalid_correction")

    def test_a_provider_failure_is_reported_not_raised(self) -> None:
        from xp_prompt import sql_candidate

        with mock.patch.object(patterns, "find", return_value=[]), mock.patch.object(
            repair.sql_candidate,
            "generate",
            side_effect=sql_candidate.SqlCandidateError("provider_error", "no answer"),
        ):
            attempt = repair.propose(
                settings(),
                question="how many users",
                schema=slice_(),
                tenant_key="default",
                message="relation does not exist",
            )
        self.assertFalse(attempt.allowed)
        self.assertEqual(attempt.reason, "generation_failed:provider_error")

    def test_an_unexpected_error_is_reported_not_raised(self) -> None:
        with mock.patch.object(patterns, "find", return_value=[]), mock.patch.object(
            repair.sql_candidate, "generate", side_effect=RuntimeError("boom")
        ):
            attempt = repair.propose(
                settings(),
                question="how many users",
                schema=slice_(),
                tenant_key="default",
                message="relation does not exist",
            )
        self.assertFalse(attempt.allowed)
        self.assertEqual(attempt.reason, "generation_failed")


class OutcomeTest(unittest.TestCase):
    def test_a_failure_is_recorded_against_the_fix(self) -> None:
        failure = {"fingerprint": "f" * 64, "kind": "unknown_identifier", "stage": "execute"}
        with mock.patch.object(
            repair.failures, "record_attempt", return_value=True
        ) as record:
            result = repair.record_outcome(
                settings(),
                tenant_key="default",
                failure=failure,
                succeeded=False,
                fix_fingerprint_value="c" * 64,
            )
        self.assertTrue(result["recorded"])
        self.assertEqual(record.call_args.kwargs["outcome"], "failed")
        self.assertEqual(record.call_args.kwargs["fix_fingerprint_value"], "c" * 64)
        self.assertFalse(result["promoted"])

    def test_a_success_on_a_pattern_is_more_evidence(self) -> None:
        failure = {"fingerprint": "f" * 64, "kind": "unknown_identifier", "stage": "execute"}
        with mock.patch.object(
            repair.failures, "record_attempt", return_value=True
        ), mock.patch.object(
            patterns,
            "record_validation",
            return_value={"id": "p1", "state": "promoted", "promoted": True},
        ) as validate:
            result = repair.record_outcome(
                settings(),
                tenant_key="default",
                failure=failure,
                succeeded=True,
                pattern_id="p1",
                fix_fingerprint_value="c" * 64,
            )
        self.assertTrue(result["promoted"])
        self.assertTrue(validate.call_args.kwargs["succeeded"])

    def test_a_generated_success_becomes_a_candidate_pattern(self) -> None:
        failure = {"fingerprint": "f" * 64, "kind": "unknown_identifier", "stage": "execute"}
        with mock.patch.object(
            repair.failures, "record_attempt", return_value=True
        ), mock.patch.object(
            patterns, "record_candidate", return_value="p9"
        ) as candidate, mock.patch.object(
            patterns, "record_validation", return_value={"id": "p9", "state": "candidate"}
        ):
            result = repair.record_outcome(
                settings(),
                tenant_key="default",
                failure=failure,
                succeeded=True,
                sql=GOOD_SQL,
                schema=slice_(),
                dialect="postgres",
            )
        candidate.assert_called_once()
        self.assertFalse(result["promoted"])
        self.assertIsNotNone(result["pattern"])

    def test_a_failed_generated_correction_does_not_create_knowledge(self) -> None:
        failure = {"fingerprint": "f" * 64, "kind": "unknown_identifier", "stage": "execute"}
        with mock.patch.object(
            repair.failures, "record_attempt", return_value=True
        ), mock.patch.object(patterns, "record_candidate") as candidate:
            result = repair.record_outcome(
                settings(),
                tenant_key="default",
                failure=failure,
                succeeded=False,
                sql=GOOD_SQL,
                schema=slice_(),
                dialect="postgres",
            )
        candidate.assert_not_called()
        self.assertIsNone(result["pattern"])


class RevalidateTest(unittest.TestCase):
    def test_an_empty_correction_is_not_validated(self) -> None:
        self.assertIsNone(
            repair._revalidate(
                "   ", schema=slice_(), database="lms", dialect="postgres", max_rows=10
            )
        )

    def test_a_valid_statement_comes_back_normalized(self) -> None:
        validated = repair._revalidate(
            GOOD_SQL, schema=slice_(), database="lms", dialect="postgres", max_rows=10
        )
        self.assertIsNotNone(validated)
        self.assertEqual(validated["dialect"], "postgres")


if __name__ == "__main__":
    unittest.main()
