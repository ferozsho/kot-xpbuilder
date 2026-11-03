"""Tests for the deterministic intent router (P2-F04)."""

from __future__ import annotations

import re
import unittest

from xp_prompt import intents

#: Every reason the router may emit, as a shape rather than a list, so a new
#: signal id is allowed but a leaked fragment of user text is not.
REASON_SHAPE = re.compile(r"^[a-z_]+(\.[a-z_]+)*$")


class VocabularyTest(unittest.TestCase):
    def test_every_planned_intent_is_routable(self) -> None:
        self.assertEqual(
            set(intents.ALL_INTENTS),
            {
                "query",
                "dataset",
                "chart",
                "dashboard",
                "explanation",
                "modification",
                "clarification",
            },
        )

    def test_every_routed_intent_has_prompt_guidance(self) -> None:
        for name in intents.INTENTS:
            self.assertIn(name, intents.PROMPT_GUIDANCE)

    def test_reasons_never_echo_the_message(self) -> None:
        decision = intents.classify(
            "Show monthly enrollment trends for Acme School",
            pending_intent="chart",
        )

        for reason in decision.reasons:
            self.assertRegex(reason, REASON_SHAPE)
            self.assertNotIn("acme", reason.lower())
        self.assertEqual(decision.reasons, tuple(decision.reasons))


class RouteTest(unittest.TestCase):
    def test_trend_question_routes_to_query(self) -> None:
        decision = intents.classify("Show monthly enrollment trends")

        self.assertEqual(decision.intent, intents.QUERY)
        self.assertFalse(decision.is_clarification)
        self.assertEqual(decision.confidence, 1.0)

    def test_dataset_request_routes_to_dataset(self) -> None:
        decision = intents.classify("Generate a dataset for active users")

        self.assertEqual(decision.intent, intents.DATASET)

    def test_chart_request_wins_over_the_query_it_contains(self) -> None:
        decision = intents.classify("Show the completion rate as a bar chart")

        self.assertEqual(decision.intent, intents.CHART)

    def test_dashboard_request_routes_to_dashboard(self) -> None:
        decision = intents.classify("Build an instructor performance dashboard")

        self.assertEqual(decision.intent, intents.DASHBOARD)

    def test_explain_verb_wins_over_the_object_it_names(self) -> None:
        decision = intents.classify("Explain this chart")

        self.assertEqual(decision.intent, intents.EXPLANATION)

    def test_why_question_routes_to_explanation(self) -> None:
        decision = intents.classify("why did enrollment drop per school")

        self.assertEqual(decision.intent, intents.EXPLANATION)

    def test_what_is_a_number_routes_to_query(self) -> None:
        decision = intents.classify("What is the average completion rate per school")

        self.assertEqual(decision.intent, intents.QUERY)

    def test_past_tense_change_is_not_an_instruction(self) -> None:
        decision = intents.classify("How many learners changed course")

        self.assertEqual(decision.intent, intents.QUERY)

    def test_short_object_word_is_enough(self) -> None:
        self.assertEqual(intents.classify("pie").intent, intents.CHART)

    def test_object_subsumption_prefers_the_narrower_intent(self) -> None:
        decision = intents.classify("list of datasets")

        self.assertEqual(decision.intent, intents.DATASET)


class ModificationTest(unittest.TestCase):
    def test_change_with_a_prior_answer_routes_to_modification(self) -> None:
        decision = intents.classify("Make it a line chart", has_prior_answer=True)

        self.assertEqual(decision.intent, intents.MODIFICATION)
        self.assertEqual(decision.target_intent, intents.CHART)

    def test_compare_this_follow_up_is_a_modification(self) -> None:
        decision = intents.classify(
            "Compare this to last year and use a line chart",
            has_prior_answer=True,
        )

        self.assertEqual(decision.intent, intents.MODIFICATION)
        self.assertEqual(decision.target_intent, intents.CHART)

    def test_instead_cue_is_a_modification(self) -> None:
        decision = intents.classify(
            "use a line chart instead", has_prior_answer=True
        )

        self.assertEqual(decision.intent, intents.MODIFICATION)

    def test_modification_without_a_draft_asks_and_parks_the_target(self) -> None:
        decision = intents.classify("Make it a line chart")

        self.assertTrue(decision.is_clarification)
        self.assertEqual(decision.pending_intent, intents.CHART)
        self.assertIn("modify.no_context", decision.reasons)
        self.assertIn("chart", decision.clarifying_question or "")


class ClarificationTest(unittest.TestCase):
    def test_unrecognised_request_asks_a_question(self) -> None:
        decision = intents.classify("hello")

        self.assertTrue(decision.is_clarification)
        self.assertEqual(decision.clarifying_question, intents.NO_SIGNAL_QUESTION)
        self.assertIsNone(decision.pending_intent)

    def test_empty_message_asks_a_question(self) -> None:
        decision = intents.classify("   ")

        self.assertTrue(decision.is_clarification)
        self.assertEqual(decision.reasons, ("empty",))

    def test_weak_signal_alone_asks_for_more_detail(self) -> None:
        decision = intents.classify("show me the data")

        self.assertTrue(decision.is_clarification)
        self.assertIn("weak_signal", decision.reasons)
        self.assertEqual(
            decision.clarifying_question, intents.WEAK_SIGNAL_QUESTION
        )

    def test_two_unrelated_objects_ask_which_one(self) -> None:
        decision = intents.classify("A dataset and a chart for enrollment")

        self.assertTrue(decision.is_clarification)
        self.assertIn("ambiguous", decision.reasons)
        self.assertIn("a dataset", decision.clarifying_question or "")
        self.assertIn("a chart", decision.clarifying_question or "")

    def test_short_reply_adopts_the_pending_intent(self) -> None:
        decision = intents.classify("yes", pending_intent=intents.CHART)

        self.assertEqual(decision.intent, intents.CHART)
        self.assertIn("context.pending.chart", decision.reasons)

    def test_refused_clarification_is_not_adopted(self) -> None:
        decision = intents.classify("no", pending_intent=intents.CHART)

        self.assertTrue(decision.is_clarification)
        self.assertEqual(decision.reasons, ("declined",))

    def test_unknown_pending_intent_is_ignored(self) -> None:
        decision = intents.classify("yes", pending_intent="teleport")

        self.assertTrue(decision.is_clarification)


if __name__ == "__main__":
    unittest.main()
