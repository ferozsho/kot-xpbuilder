"""Tests for failure classification and repair budgets (P5-F01/F06/F07).

The two properties that matter: the same failure is always classified the same way,
and a policy refusal is never treated as something to repair.
"""

from __future__ import annotations

import unittest

from xp_prompt import failures


def classify(**overrides):
    arguments = {"stage": "execute"}
    arguments.update(overrides)
    return failures.classify(**arguments)


class ClassificationTest(unittest.TestCase):
    def test_postgres_sqlstates_map_to_kinds(self) -> None:
        cases = {
            "42601": failures.SYNTAX_ERROR,
            "42703": failures.UNKNOWN_IDENTIFIER,
            "42P01": failures.UNKNOWN_IDENTIFIER,
            "42501": failures.PERMISSION_DENIED,
            "57014": failures.TIMEOUT,
            "22003": failures.TYPE_ERROR,
        }
        for sqlstate, kind in cases.items():
            with self.subTest(sqlstate=sqlstate):
                self.assertEqual(classify(sqlstate=sqlstate).kind, kind)

    def test_mysql_errnos_map_to_the_same_kinds(self) -> None:
        cases = {"1064": failures.SYNTAX_ERROR, "1054": failures.UNKNOWN_IDENTIFIER}
        for errno, kind in cases.items():
            with self.subTest(errno=errno):
                self.assertEqual(classify(errno=errno).kind, kind)

    def test_provider_codes_are_understood(self) -> None:
        self.assertEqual(classify(kind_hint="quota_exceeded").kind, failures.QUOTA)
        self.assertEqual(
            classify(kind_hint="structured_output_invalid").kind,
            failures.MALFORMED_RESPONSE,
        )

    def test_messages_are_a_fallback_only(self) -> None:
        self.assertEqual(
            classify(message="canceling statement due to statement timeout").kind,
            failures.TIMEOUT,
        )
        self.assertEqual(
            classify(message="column revenue does not exist").kind,
            failures.UNKNOWN_IDENTIFIER,
        )

    def test_a_connection_error_is_an_outage(self) -> None:
        self.assertEqual(
            classify(error_class="OperationalError").kind, failures.UNAVAILABLE
        )

    def test_an_unrecognised_failure_is_unknown_not_retryable(self) -> None:
        event = classify(message="something entirely unexpected happened")

        self.assertEqual(event.kind, failures.UNKNOWN)
        self.assertFalse(event.retryable)

    def test_the_stage_is_normalised(self) -> None:
        self.assertEqual(classify(stage="PLAN").stage, "plan")
        self.assertEqual(classify(stage="nonsense").stage, "execute")

    def test_a_timeout_at_execute_is_the_database_not_the_model(self) -> None:
        self.assertEqual(classify(sqlstate="57014").category, "system")
        self.assertEqual(
            classify(stage="candidate", kind_hint="timeout").category, "provider"
        )


class FingerprintTest(unittest.TestCase):
    def test_the_same_failure_has_the_same_fingerprint(self) -> None:
        first = classify(sqlstate="42703", message="column 'a' does not exist")
        second = classify(sqlstate="42703", message="column 'b' does not exist")

        self.assertEqual(first.fingerprint, second.fingerprint)

    def test_a_different_kind_is_a_different_fingerprint(self) -> None:
        self.assertNotEqual(
            classify(sqlstate="42601").fingerprint, classify(sqlstate="42703").fingerprint
        )

    def test_the_fingerprint_carries_no_content(self) -> None:
        event = classify(message="column 'tenant_secret' does not exist")

        self.assertEqual(len(event.fingerprint), 64)
        self.assertNotIn("tenant_secret", event.fingerprint)

    def test_the_detail_is_masked(self) -> None:
        event = classify(message="column 'revenue' does not exist at line 42")

        self.assertNotIn("revenue", event.detail)
        self.assertNotIn("42", event.detail)

    def test_the_same_message_shape_agrees_without_a_code(self) -> None:
        first = classify(message="column 'a' does not exist")
        second = classify(message="column 'b' does not exist")

        self.assertEqual(first.fingerprint, second.fingerprint)


class TerminalPolicyTest(unittest.TestCase):
    def test_policy_refusals_are_terminal_and_never_retried(self) -> None:
        for kind in failures.TERMINAL_KINDS:
            with self.subTest(kind=kind):
                event = classify(kind_hint=kind)
                allowed, reason = failures.can_repair(
                    event, failures.RepairState(), failures.RepairBudget()
                )
                self.assertTrue(event.terminal)
                self.assertFalse(allowed)
                self.assertEqual(reason, "terminal_policy")

    def test_a_permission_sqlstate_is_terminal(self) -> None:
        event = classify(sqlstate="42501")

        self.assertTrue(event.terminal)
        self.assertFalse(event.retryable)


class BudgetTest(unittest.TestCase):
    def event(self):
        return classify(sqlstate="42601")

    def test_a_repairable_failure_is_allowed_once(self) -> None:
        allowed, reason = failures.can_repair(
            self.event(), failures.RepairState(), failures.RepairBudget()
        )

        self.assertTrue(allowed)
        self.assertEqual(reason, "allowed")

    def test_attempts_are_capped(self) -> None:
        allowed, reason = failures.can_repair(
            self.event(),
            failures.RepairState(attempts=2),
            failures.RepairBudget(max_attempts=2),
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "attempts_exhausted")

    def test_the_deadline_is_enforced(self) -> None:
        allowed, reason = failures.can_repair(
            self.event(),
            failures.RepairState(elapsed_ms=30_000),
            failures.RepairBudget(max_elapsed_ms=30_000),
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "deadline_exceeded")

    def test_the_token_budget_is_enforced(self) -> None:
        allowed, reason = failures.can_repair(
            self.event(),
            failures.RepairState(tokens=4_000),
            failures.RepairBudget(max_tokens=4_000),
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "token_budget_exhausted")

    def test_a_non_retryable_kind_is_refused_before_the_budget(self) -> None:
        allowed, reason = failures.can_repair(
            classify(kind_hint="unknown"),
            failures.RepairState(),
            failures.RepairBudget(),
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "not_retryable")

    def test_the_budget_and_state_are_reported_publicly(self) -> None:
        payload = failures.RepairBudget().to_public_dict()

        self.assertEqual(
            sorted(payload),
            ["max_attempts", "max_elapsed_ms", "max_tokens", "suppress_failed_fixes"],
        )
        self.assertEqual(
            sorted(failures.RepairState().to_public_dict()),
            ["attempts", "elapsed_ms", "tokens"],
        )


class FixFingerprintTest(unittest.TestCase):
    def test_whitespace_and_case_do_not_change_the_fix_identity(self) -> None:
        self.assertEqual(
            failures.fix_fingerprint("SELECT  1  FROM t"),
            failures.fix_fingerprint("select 1 from t"),
        )

    def test_a_different_fix_is_a_different_identity(self) -> None:
        self.assertNotEqual(
            failures.fix_fingerprint("select 1"), failures.fix_fingerprint("select 2")
        )

    def test_an_empty_fix_has_no_identity(self) -> None:
        self.assertEqual(failures.fix_fingerprint("   "), "")


class SettingsBudgetTest(unittest.TestCase):
    def test_the_budget_comes_from_settings(self) -> None:
        class Settings:
            repair_max_attempts = 1
            repair_max_elapsed_ms = 5_000
            repair_max_tokens = 500

        budget = failures.budget_from_settings(Settings())

        self.assertEqual(budget.max_attempts, 1)
        self.assertEqual(budget.max_elapsed_ms, 5_000)
        self.assertEqual(budget.max_tokens, 500)


if __name__ == "__main__":
    unittest.main()
