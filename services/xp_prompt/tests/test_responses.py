"""Tests for the structured response block contract (P2-F06)."""

from __future__ import annotations

import unittest

from xp_prompt import intents, responses


class VocabularyTest(unittest.TestCase):
    def test_every_planned_block_type_is_declared(self) -> None:
        self.assertEqual(
            set(responses.BLOCK_TYPES),
            {
                "clarification",
                "plan",
                "assumptions",
                "sql_preview",
                "result_preview",
                "dataset_profile",
                "chart_recommendation",
                "dashboard_proposal",
                "warnings",
                "repair",
                "links",
            },
        )

    def test_unknown_types_are_dropped(self) -> None:
        self.assertEqual(
            responses.normalize_blocks([{"type": "hologram", "x": 1}]),
            [],
        )

    def test_non_list_input_is_empty(self) -> None:
        for value in (None, {}, "text", 7):
            self.assertEqual(responses.normalize_blocks(value), [])

    def test_garbage_entries_never_raise(self) -> None:
        blocks = responses.normalize_blocks(
            [
                None,
                42,
                {},
                {"type": "plan"},
                {"type": "plan", "items": "not a list"},
                {"type": "result_preview", "columns": [], "rows": []},
                {"type": "plan", "items": ["ok"]},
            ]
        )

        self.assertEqual([block["type"] for block in blocks], ["plan"])

    def test_block_count_is_capped(self) -> None:
        blocks = responses.normalize_blocks(
            [{"type": "warnings", "items": ["w"]}] * (responses.MAX_BLOCKS + 5)
        )

        self.assertEqual(len(blocks), responses.MAX_BLOCKS)


class NormalizationTest(unittest.TestCase):
    def test_items_and_text_are_bounded(self) -> None:
        block = responses.normalize_blocks(
            [
                {
                    "type": "plan",
                    "title": "t" * 5000,
                    "items": ["item"] * (responses.MAX_ITEMS + 10),
                }
            ]
        )[0]

        self.assertEqual(len(block["items"]), responses.MAX_ITEMS)
        self.assertLessEqual(len(block["title"]), responses.MAX_TEXT_CHARS)

    def test_sql_is_truncated_and_flagged(self) -> None:
        block = responses.normalize_blocks(
            [{"type": "sql_preview", "sql": "SELECT " + "1, " * 4000}]
        )[0]

        self.assertTrue(block["truncated"])
        self.assertLessEqual(len(block["sql"]), responses.MAX_SQL_CHARS)

    def test_short_sql_is_not_flagged(self) -> None:
        block = responses.normalize_blocks(
            [{"type": "sql_preview", "sql": "select 1", "dialect": "postgres"}]
        )[0]

        self.assertFalse(block["truncated"])
        self.assertEqual(block["dialect"], "postgres")

    def test_result_preview_caps_rows_and_cells(self) -> None:
        block = responses.normalize_blocks(
            [
                {
                    "type": "result_preview",
                    "columns": ["a", "b"],
                    "rows": [["x" * 500, 1]] * (responses.MAX_ROWS + 5),
                    "row_count": 500,
                }
            ]
        )[0]

        self.assertEqual(len(block["rows"]), responses.MAX_ROWS)
        self.assertEqual(block["row_count"], 500)
        self.assertTrue(block["truncated"])
        self.assertLessEqual(len(block["rows"][0][0]), responses.MAX_CELL_CHARS)

    def test_result_preview_marks_truncation_from_row_count(self) -> None:
        block = responses.normalize_blocks(
            [
                {
                    "type": "result_preview",
                    "columns": ["a"],
                    "rows": [["1"], ["2"]],
                    "row_count": 99,
                }
            ]
        )[0]

        self.assertTrue(block["truncated"])

    def test_dataset_profile_requires_named_columns(self) -> None:
        blocks = responses.normalize_blocks(
            [
                {"type": "dataset_profile", "columns": [{"type": "int"}]},
                {
                    "type": "dataset_profile",
                    "table": "mdl_user",
                    "columns": [{"name": "id", "type": "int", "nullable": False}],
                },
            ]
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["columns"][0]["name"], "id")
        self.assertFalse(blocks[0]["columns"][0]["nullable"])

    def test_chart_recommendation_requires_a_type(self) -> None:
        blocks = responses.normalize_blocks(
            [
                {"type": "chart_recommendation", "x": "month"},
                {
                    "type": "chart_recommendation",
                    "chart_type": "line",
                    "x": "month",
                    "y": ["enrolments"],
                },
            ]
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["chart_type"], "line")

    def test_dashboard_proposal_requires_sections(self) -> None:
        blocks = responses.normalize_blocks(
            [
                {"type": "dashboard_proposal", "sections": []},
                {
                    "type": "dashboard_proposal",
                    "title": "Instructor performance",
                    "sections": [{"title": "Overview", "items": ["completion rate"]}],
                },
            ]
        )

        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["sections"][0]["title"], "Overview")

    def test_repair_outcome_is_normalized(self) -> None:
        block = responses.normalize_blocks(
            [{"type": "repair", "outcome": "exploded", "attempts": "2"}]
        )[0]

        self.assertEqual(block["outcome"], "unchanged")
        self.assertEqual(block["attempts"], 2)

    def test_empty_items_are_dropped(self) -> None:
        self.assertEqual(responses.normalize_blocks([{"type": "warnings", "items": []}]), [])


class AnswerBlocksTest(unittest.TestCase):
    def test_dataset_intent_prepares_a_plan_and_a_warning(self) -> None:
        blocks = responses.answer_blocks(intents.DATASET)

        self.assertEqual([block["type"] for block in blocks], ["plan", "warnings"])
        self.assertTrue(blocks[0]["items"])
        self.assertIn("Nothing has been saved", blocks[1]["items"][0])

    def test_chart_and_dashboard_intents_also_plan(self) -> None:
        for intent in (intents.CHART, intents.DASHBOARD):
            types = [block["type"] for block in responses.answer_blocks(intent)]
            self.assertEqual(types, ["plan", "warnings"])

    def test_query_without_a_period_states_the_assumption(self) -> None:
        blocks = responses.answer_blocks(intents.QUERY, content="show enrollment trends")

        self.assertEqual([block["type"] for block in blocks], ["assumptions"])
        self.assertEqual(len(blocks[0]["items"]), 2)
        self.assertIn("No time range", blocks[0]["items"][0])

    def test_query_with_a_period_does_not_assume_one(self) -> None:
        blocks = responses.answer_blocks(intents.QUERY, content="show trends last month")

        self.assertEqual(len(blocks[0]["items"]), 1)
        self.assertIn("No query has been run", blocks[0]["items"][0])

    def test_modification_notes_what_it_applies_to(self) -> None:
        blocks = responses.answer_blocks(
            intents.MODIFICATION, has_prior_answer=True
        )

        self.assertEqual(blocks[0]["type"], "assumptions")
        self.assertIn("most recent answer", blocks[0]["items"][0])

    def test_explanation_needs_no_blocks(self) -> None:
        self.assertEqual(responses.answer_blocks(intents.EXPLANATION), [])

    def test_clarification_block_carries_the_question(self) -> None:
        blocks = responses.clarification_blocks("Which metric?")

        self.assertEqual(blocks, [{"type": "clarification", "question": "Which metric?"}])


class LinksTest(unittest.TestCase):
    def test_only_known_entities_with_safe_ids_survive(self) -> None:
        block = responses.normalize_blocks(
            [
                {
                    "type": "links",
                    "items": [
                        {"entity_type": "chart", "entity_id": "42"},
                        {"entity_type": "user", "entity_id": "1"},
                        {"entity_type": "dataset", "entity_id": "../../etc/passwd"},
                        {"label": "no entity", "entity_id": "7"},
                    ],
                }
            ]
        )[0]

        self.assertEqual(len(block["items"]), 1)
        self.assertEqual(block["items"][0]["entity_type"], "chart")
        self.assertEqual(block["items"][0]["entity_id"], "42")
        self.assertEqual(block["items"][0]["label"], "Open chart")

    def test_links_need_at_least_one_valid_item(self) -> None:
        self.assertEqual(
            responses.normalize_blocks(
                [
                    {
                        "type": "links",
                        "items": [{"entity_type": "nope", "entity_id": "1"}],
                    }
                ]
            ),
            [],
        )

    def test_link_count_is_capped(self) -> None:
        block = responses.normalize_blocks(
            [
                {
                    "type": "links",
                    "items": [{"entity_type": "chart", "entity_id": str(n)} for n in range(20)],
                }
            ]
        )[0]

        self.assertEqual(len(block["items"]), responses.MAX_LINKS)

    def test_no_url_is_ever_stored(self) -> None:
        block = responses.normalize_blocks(
            [
                {
                    "type": "links",
                    "items": [
                        {
                            "entity_type": "dashboard",
                            "entity_id": "7",
                            "label": "Open dashboard",
                            "href": "https://evil.example/steal",
                        }
                    ],
                }
            ]
        )[0]

        self.assertNotIn("href", block["items"][0])

    def test_native_link_helper_rejects_bad_input(self) -> None:
        self.assertEqual(
            responses.native_link("chart", 12),
            {"entity_type": "chart", "entity_id": "12"},
        )
        with self.assertRaises(ValueError):
            responses.native_link("user", 1)
        with self.assertRaises(ValueError):
            responses.native_link("chart", "../x")


if __name__ == "__main__":
    unittest.main()
