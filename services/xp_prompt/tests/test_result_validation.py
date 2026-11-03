"""Tests for result validation and preview blocks (P4-F10)."""

from __future__ import annotations

import unittest

from xp_prompt import result_validation


class ShapeTest(unittest.TestCase):
    def test_a_normal_result_is_usable(self) -> None:
        verdict = result_validation.validate_result(
            ["course", "learners"], [["Maths", 12], ["Physics", 7]]
        )

        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.columns, ("course", "learners"))
        self.assertEqual(verdict.returned, 2)
        self.assertEqual(verdict.row_count, 2)
        self.assertFalse(verdict.truncated)

    def test_no_columns_is_refused(self) -> None:
        for columns in ([], None, "course"):
            with self.subTest(columns=columns):
                with self.assertRaises(result_validation.ResultValidationError) as raised:
                    result_validation.validate_result(columns, [])
                self.assertEqual(raised.exception.code, "no_columns")

    def test_short_rows_are_padded_and_long_rows_trimmed(self) -> None:
        verdict = result_validation.validate_result(
            ["a", "b"], [["1"], ["1", "2", "3"]]
        )

        self.assertEqual(verdict.rows, (("1", ""), ("1", "2")))

    def test_cells_are_strings_and_nulls_are_empty(self) -> None:
        verdict = result_validation.validate_result(
            ["a", "b", "c"], [[1, None, float("nan")]]
        )

        self.assertEqual(verdict.rows[0], ("1", "", ""))

    def test_row_count_and_truncation_are_honest(self) -> None:
        verdict = result_validation.validate_result(
            ["a"], [["1"], ["2"]], row_count=99
        )

        self.assertEqual(verdict.row_count, 99)
        self.assertEqual(verdict.returned, 2)
        self.assertTrue(verdict.truncated)
        self.assertIn("truncated", verdict.issues)

    def test_preview_size_is_capped(self) -> None:
        rows = [["x"] for _ in range(result_validation.MAX_PREVIEW_ROWS + 20)]

        verdict = result_validation.validate_result(["a"], rows, max_rows=5)

        self.assertEqual(len(verdict.rows), 5)
        self.assertTrue(verdict.truncated)


class WarningTest(unittest.TestCase):
    def test_empty_result_is_usable_but_reported(self) -> None:
        verdict = result_validation.validate_result(["course"], [])

        self.assertTrue(verdict.ok)
        self.assertIn("empty_result", verdict.issues)
        warnings = [b for b in verdict.blocks if b["type"] == "warnings"][0]
        self.assertIn("returned no rows", warnings["items"][0])

    def test_wide_result_drops_extra_columns(self) -> None:
        columns = [f"c{index}" for index in range(10)]
        rows = [[str(index) for index in range(10)]]

        verdict = result_validation.validate_result(columns, rows, max_columns=3)

        self.assertEqual(len(verdict.columns), 3)
        self.assertEqual(len(verdict.rows[0]), 3)
        self.assertIn("wide_result", verdict.issues)

    def test_all_null_column_is_flagged(self) -> None:
        verdict = result_validation.validate_result(
            ["course", "empty"], [["Maths", ""], ["Physics", ""]]
        )

        self.assertIn("all_null:empty", verdict.issues)
        self.assertNotIn("all_null:course", verdict.issues)

    def test_chart_intent_without_a_measure_is_flagged(self) -> None:
        verdict = result_validation.validate_result(
            ["course"], [["Maths"], ["Physics"]], intent="chart"
        )

        self.assertIn("no_measure", verdict.issues)

    def test_chart_intent_with_a_measure_is_fine(self) -> None:
        verdict = result_validation.validate_result(
            ["course", "learners"], [["Maths", "12"]], intent="chart"
        )

        self.assertNotIn("no_measure", verdict.issues)

    def test_query_intent_does_not_require_a_measure(self) -> None:
        verdict = result_validation.validate_result(
            ["course"], [["Maths"]], intent="query"
        )

        self.assertNotIn("no_measure", verdict.issues)


class MaskingTest(unittest.TestCase):
    def test_sensitive_columns_are_masked(self) -> None:
        verdict = result_validation.validate_result(
            ["user", "password", "api_key"],
            [["alice", "hunter2", "sk-live-123"]],
        )

        self.assertEqual(verdict.rows[0], ("alice", "[redacted]", "[redacted]"))
        self.assertIn("masked_columns", verdict.issues)
        rendered = str(verdict.to_public_dict())
        self.assertNotIn("hunter2", rendered)
        self.assertNotIn("sk-live-123", rendered)

    def test_ordinary_names_are_not_masked(self) -> None:
        verdict = result_validation.validate_result(
            ["token_count", "user"], [["3", "alice"]]
        )

        self.assertEqual(verdict.rows[0], ("3", "alice"))

    def test_masked_column_is_not_also_reported_as_null(self) -> None:
        verdict = result_validation.validate_result(["password"], [["x"], ["y"]])

        self.assertFalse(any(issue.startswith("all_null") for issue in verdict.issues))


class BlockTest(unittest.TestCase):
    def test_blocks_follow_the_response_contract(self) -> None:
        verdict = result_validation.validate_result(
            ["course", "learners"], [["Maths", 12]]
        )

        # No warnings for a clean result, so only the preview block is emitted.
        self.assertEqual([block["type"] for block in verdict.blocks], ["result_preview"])
        preview = verdict.blocks[0]
        self.assertEqual(preview["columns"], ["course", "learners"])
        self.assertEqual(preview["row_count"], 1)
        self.assertFalse(preview["truncated"])

    def test_a_warning_block_accompanies_an_imperfect_result(self) -> None:
        verdict = result_validation.validate_result(["course"], [])

        self.assertEqual(
            [block["type"] for block in verdict.blocks],
            ["result_preview", "warnings"],
        )

    def test_public_dict_round_trips_json(self) -> None:
        import json

        payload = result_validation.validate_result(
            ["a"], [["1"]], row_count=1
        ).to_public_dict()

        self.assertEqual(json.loads(json.dumps(payload))["columns"], ["a"])


if __name__ == "__main__":
    unittest.main()
