"""Tests for dashboard layout and native filter compatibility (P8-F04/F05)."""

from __future__ import annotations

import unittest

from xp_prompt import dashboard_layout


SECTION_OF_FAMILY = {
    "single_value": "headline",
    "correlation": "trend",
    "part_to_whole": "comparison",
    "detail": "detail",
    "matrix": "matrix",
}


def slot(slot_id: str, chart_id: int | None, family: str = "trend", **extra) -> dict:
    body = {
        "id": slot_id,
        "chart_id": chart_id,
        "title": f"chart {chart_id}",
        "family": family,
        "section": extra.pop("section", SECTION_OF_FAMILY.get(family, "trend")),
        "dataset_id": extra.pop("dataset_id", 7),
        "dataset_name": extra.pop("dataset_name", "student_scores"),
    }
    body.update(extra)
    return body


class PositionTest(unittest.TestCase):
    def test_two_half_width_charts_share_one_row(self) -> None:
        position, problems = dashboard_layout.build_position(
            [slot("7:trend", 11), slot("9:trend", 12)], title="Scores"
        )
        self.assertEqual(problems, ())
        self.assertEqual(position["GRID_ID"]["children"], ["ROW-1"])
        row = position["ROW-1"]
        self.assertEqual(row["children"], ["CHART-1", "CHART-2"])
        self.assertEqual(position["CHART-1"]["meta"]["width"], 6)
        self.assertEqual(position["CHART-2"]["meta"]["width"], 6)
        self.assertEqual(position["HEADER_ID"]["meta"]["text"], "Scores")

    def test_full_width_family_takes_its_own_row(self) -> None:
        position, _ = dashboard_layout.build_position(
            [slot("7:trend", 11), slot("7:detail", 12, family="detail")]
        )
        self.assertEqual(len(position["GRID_ID"]["children"]), 2)
        self.assertEqual(position["CHART-2"]["meta"]["width"], dashboard_layout.GRID_COLUMNS)

    def test_chart_without_saved_id_is_dropped_with_a_problem(self) -> None:
        position, problems = dashboard_layout.build_position(
            [slot("7:trend", None), slot("7:detail", 12, family="detail")]
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("no saved chart id", problems[0])
        self.assertEqual(len(position["GRID_ID"]["children"]), 1)

    def test_duplicate_chart_is_placed_once(self) -> None:
        _, problems = dashboard_layout.build_position(
            [slot("7:trend", 11), slot("9:trend", 11)]
        )
        self.assertEqual(len(problems), 1)
        self.assertIn("proposed twice", problems[0])

    def test_generated_position_validates_clean(self) -> None:
        position, _ = dashboard_layout.build_position(
            [
                slot("7:single_value", 21, family="single_value"),
                slot("7:trend", 22),
                slot("9:trend", 23, dataset_id=9),
            ]
        )
        self.assertEqual(dashboard_layout.validate_position(position), ())

    def test_validation_catches_a_broken_row(self) -> None:
        position, _ = dashboard_layout.build_position([slot("7:trend", 11)])
        position["ROW-1"]["children"].append("CHART-99")
        problems = dashboard_layout.validate_position(position)
        self.assertTrue(any("missing child CHART-99" in item for item in problems))

    def test_validation_catches_row_overflow(self) -> None:
        position, _ = dashboard_layout.build_position(
            [slot("7:trend", 11), slot("9:trend", 12)]
        )
        position["ROW-1"]["children"].append("CHART-3")
        position["CHART-3"] = {
            "type": "CHART",
            "id": "CHART-3",
            "children": [],
            "parents": ["ROOT_ID", "GRID_ID", "ROW-1"],
            "meta": {"chartId": 13, "width": 6, "height": 50, "sliceName": "third"},
        }
        problems = dashboard_layout.validate_position(position)
        self.assertTrue(any("over 12" in item for item in problems))

    def test_validation_catches_a_duplicated_chart_id(self) -> None:
        position, _ = dashboard_layout.build_position(
            [slot("7:trend", 11), slot("9:trend", 12)]
        )
        position["CHART-2"]["meta"]["chartId"] = 11
        problems = dashboard_layout.validate_position(position)
        self.assertTrue(any("placed more than once" in item for item in problems))

    def test_validation_catches_wrong_parents(self) -> None:
        position, _ = dashboard_layout.build_position([slot("7:trend", 11)])
        position["CHART-1"]["parents"] = ["ROOT_ID", "GRID_ID"]
        problems = dashboard_layout.validate_position(position)
        self.assertTrue(any("instead of" in item for item in problems))

    def test_non_object_position_is_refused(self) -> None:
        self.assertEqual(
            dashboard_layout.validate_position([]), ("position is not an object",)
        )


class FilterTest(unittest.TestCase):
    def test_filter_needs_two_charts_to_have_a_scope(self) -> None:
        plan = dashboard_layout.plan_filters(
            [slot("7:trend", 11)], columns={7: {"course_name": "categorical"}}
        )
        self.assertEqual(plan.filters, ())
        self.assertEqual(plan.rejected[0].reason, "single_chart_scope")

    def test_shared_categorical_column_becomes_a_select_filter(self) -> None:
        plan = dashboard_layout.plan_filters(
            [slot("7:trend", 11), slot("7:detail", 12, family="detail")],
            columns={7: {"course_name": "categorical"}},
        )
        self.assertEqual(len(plan.filters), 1)
        payload = plan.filters[0]
        self.assertEqual(payload["filterType"], dashboard_layout.FILTER_SELECT)
        self.assertEqual(payload["targets"][0]["datasetId"], 7)
        self.assertEqual(payload["targets"][0]["column"]["name"], "course_name")
        self.assertEqual(
            payload["scope"]["rootPath"], list(dashboard_layout.FILTER_SCOPE)
        )
        self.assertTrue(payload["controlValues"]["multiSelect"])

    def test_temporal_column_becomes_a_time_filter(self) -> None:
        plan = dashboard_layout.plan_filters(
            [slot("7:trend", 11), slot("7:detail", 12, family="detail")],
            columns={7: {"created_at": "temporal"}},
        )
        self.assertEqual(plan.filters[0]["filterType"], dashboard_layout.FILTER_TIME)
        self.assertEqual(plan.filters[0]["controlValues"], {})

    def test_dataset_without_declared_columns_is_refused(self) -> None:
        plan = dashboard_layout.plan_filters(
            [slot("7:trend", 11), slot("7:detail", 12, family="detail")], columns={}
        )
        self.assertEqual(plan.filters, ())
        self.assertEqual(plan.rejected[0].reason, "columns_not_declared")

    def test_filter_budget_is_enforced_and_reported(self) -> None:
        columns = {7: {"course_name": "categorical", "created_at": "temporal"}}
        plan = dashboard_layout.plan_filters(
            [slot("7:trend", 11), slot("7:detail", 12, family="detail")],
            columns=columns,
            max_filters=1,
        )
        self.assertEqual(len(plan.filters), 1)
        self.assertTrue(any(item.reason == "filter_budget" for item in plan.rejected))

    def test_time_filter_wins_the_budget_and_budget_is_reported_once(self) -> None:
        columns = {
            7: {
                "active_flag": "categorical",
                "created_at": "temporal",
                "course_name": "categorical",
            }
        }
        plan = dashboard_layout.plan_filters(
            [slot("7:trend", 11), slot("7:detail", 12, family="detail")],
            columns=columns,
            max_filters=1,
        )
        self.assertEqual(len(plan.filters), 1)
        self.assertEqual(plan.filters[0]["name"], "created_at")
        self.assertEqual(
            [item.reason for item in plan.rejected], ["filter_budget"]
        )

    def test_generated_slots_do_not_count_toward_filter_scope(self) -> None:
        plan = dashboard_layout.plan_filters(
            [
                slot("7:trend", 11),
                slot("7:detail", None, family="detail"),
            ],
            columns={7: {"course_name": "categorical"}},
        )
        self.assertEqual(plan.filters, ())
        self.assertEqual(plan.rejected[0].reason, "single_chart_scope")

    def test_public_shape_is_stable(self) -> None:
        plan = dashboard_layout.plan_filters(
            [slot("7:trend", 11), slot("7:detail", 12, family="detail")],
            columns={7: {"course_name": "categorical"}},
        )
        body = plan.to_public_dict()
        self.assertEqual(body["count"], 1)
        self.assertIn("native_filters", body)
        self.assertIsInstance(body["rejected"], list)


if __name__ == "__main__":
    unittest.main()
