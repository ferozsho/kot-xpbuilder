"""Tests for question planning (P4-F01).

The bar for interrupting a user is high: only a reference the schema cannot satisfy,
or a genuine choice between measures, may block. Everything else must stay a note.
"""

from __future__ import annotations

import unittest

from xp_prompt import question_plan, schema_slice


def dataset(**overrides):
    entry = {
        "name": "Student Overview",
        "schema": "public",
        "database": "File uploads",
        "dialect": "postgres",
        "sql": "SELECT 1",
        "columns": [
            {"name": "student_id", "type": "STRING"},
            {"name": "completion_status", "type": "STRING"},
            {"name": "assessment_score", "type": "INTEGER"},
            {"name": "course_progress", "type": "DOUBLE PRECISION"},
            {"name": "last_activity_date", "type": "DATE", "temporal": True},
        ],
    }
    entry.update(overrides)
    return entry


def slice_(datasets=None):
    return schema_slice.SchemaSlice.from_payload(
        {"datasets": datasets if datasets is not None else [dataset()]}
    )


def plan(question, schema=None):
    return question_plan.analyse(question, schema or slice_())


class MeasureAndDimensionTest(unittest.TestCase):
    def test_a_named_measure_is_recognised(self) -> None:
        result = plan("What is the average assessment score?")

        self.assertEqual(result.measures, ("assessment_score",))
        self.assertEqual(result.dimensions, ())

    def test_a_named_dimension_is_recognised(self) -> None:
        result = plan("Assessment score by completion status")

        self.assertEqual(result.measures, ("assessment_score",))
        self.assertEqual(result.dimensions, ("completion_status",))

    def test_a_count_question_does_not_invent_a_measure(self) -> None:
        """"How many students" is a row count, not a SUM of whatever is numeric."""
        result = plan("How many students per course?")

        self.assertEqual(result.measures, ())
        self.assertIn("row count", result.prompt_block())

    def test_a_value_question_may_infer_the_only_numeric_column(self) -> None:
        schema = slice_(
            [dataset(columns=[{"name": "score", "type": "INTEGER"}])]
        )

        result = plan("Total per month", schema)

        self.assertEqual(result.measures, ("score",))
        self.assertEqual(result.grain, "month")


class AmbiguityTest(unittest.TestCase):
    def test_a_reference_the_schema_cannot_satisfy_blocks(self) -> None:
        result = plan("How much revenue did we make?")

        self.assertEqual(result.ambiguous, (question_plan.UNKNOWN_REFERENCE,))
        self.assertEqual(result.blocking, (question_plan.UNKNOWN_REFERENCE,))
        self.assertIn("could not match", result.clarification())

    def test_two_unrelated_measures_with_nothing_to_choose_between_blocks(self) -> None:
        result = plan("Total per month")

        self.assertEqual(result.ambiguous, (question_plan.MEASURE_AMBIGUOUS,))
        self.assertIn("Which number", result.clarification())

    def test_a_named_measure_is_never_ambiguous(self) -> None:
        result = plan("What is the total assessment score?")

        self.assertEqual(result.blocking, ())

    def test_no_measure_and_no_dimension_is_not_an_error(self) -> None:
        result = plan("Show me the data")

        self.assertEqual(result.blocking, ())

    def test_an_empty_question_yields_an_empty_plan(self) -> None:
        self.assertEqual(plan("").to_public_dict()["blocking"], [])
        self.assertEqual(plan("   ").measures, ())


class GrainAndComparisonTest(unittest.TestCase):
    def test_time_words_become_a_grain(self) -> None:
        for text, grain in (
            ("Students per day", "day"),
            ("Weekly signups", "week"),
            ("Monthly completion", "month"),
            ("Quarterly totals", "quarter"),
            ("Yearly trend", "year"),
        ):
            with self.subTest(text=text):
                self.assertEqual(plan(text).grain, grain)

    def test_comparison_words_are_detected(self) -> None:
        result = plan("Compare assessment score this month versus last month")

        self.assertEqual(result.comparison, "previous_period")

    def test_a_grain_without_a_date_column_is_a_note_not_a_refusal(self) -> None:
        schema = slice_(
            [dataset(columns=[{"name": "score", "type": "INTEGER"}])]
        )

        result = plan("Monthly totals", schema)

        self.assertEqual(result.blocking, ())
        self.assertTrue(any("date column" in note for note in result.notes))


class FilterTest(unittest.TestCase):
    def test_an_explicit_filter_is_captured(self) -> None:
        result = plan("How many students where completion status is Active")

        self.assertTrue(result.filters)
        self.assertIn("completion_status", result.filters[0])


class PromptBlockTest(unittest.TestCase):
    def test_the_block_lists_what_was_understood(self) -> None:
        block = plan("Assessment score by completion status per month").prompt_block()

        self.assertIn("assessment_score", block)
        self.assertIn("completion_status", block)
        self.assertIn("month", block)

    def test_an_empty_plan_produces_no_block(self) -> None:
        self.assertEqual(plan("").prompt_block(), "")

    def test_the_public_payload_is_metadata_only(self) -> None:
        payload = plan("Assessment score by completion status").to_public_dict()

        self.assertEqual(
            sorted(payload),
            [
                "ambiguous",
                "blocking",
                "comparison",
                "dimensions",
                "filters",
                "grain",
                "measures",
            ],
        )


if __name__ == "__main__":
    unittest.main()
