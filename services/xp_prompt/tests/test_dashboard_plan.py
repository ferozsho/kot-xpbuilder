"""Tests for the dashboard planner (P8-F01/F02/F03)."""

from __future__ import annotations

import unittest

from xp_prompt import chart_catalog, dashboard_plan

ALL_VIZ = [
    "echarts_timeseries_line",
    "echarts_timeseries_bar",
    "echarts_timeseries_scatter",
    "pie",
    "heatmap_v2",
    "big_number_total",
    "table",
]


def catalog(available=None) -> chart_catalog.Catalog:
    return chart_catalog.Catalog.from_payload(
        {"viz_types": available or ALL_VIZ, "denylist": []}
    )


def student_dataset() -> dict:
    return {
        "id": 7,
        "name": "student_scores",
        "columns": [
            {"name": "student_name"},
            {"name": "course_name"},
            {"name": "score"},
            {"name": "created_at"},
        ],
    }


def course_dataset() -> dict:
    return {
        "id": 9,
        "name": "course_enrolments",
        "columns": [{"name": "course_name"}, {"name": "enrolled"}, {"name": "month"}],
    }


def line_candidate(score: float = 0.9) -> dict:
    return {
        "viz_type": "echarts_timeseries_line",
        "family": "trend",
        "title": "Scores over time",
        "form_data": {"viz_type": "echarts_timeseries_line", "x_axis": "created_at"},
        "score": score,
        "rationale": "a temporal column and a measure are present",
    }


def pie_candidate(score: float = 0.45) -> dict:
    return {
        "viz_type": "pie",
        "family": "part_to_whole",
        "title": "Score share by course",
        "form_data": {"viz_type": "pie", "groupby": "course_name", "metric": "score"},
        "score": score,
        "rationale": "a low-cardinality category can carry a part-to-whole read",
    }


class EmptyGoalTest(unittest.TestCase):
    def test_plan_is_built_from_available_artifacts_and_says_so(self) -> None:
        plan = dashboard_plan.build(
            datasets=[student_dataset()],
            charts=[
                {"id": 41, "name": "Scores", "viz_type": "table", "dataset_id": 7}
            ],
        )
        self.assertEqual(plan.title, "student_scores overview")
        self.assertEqual(plan.audience, "unspecified")
        self.assertEqual(len(plan.slots), 1)
        self.assertTrue(any("named no dataset" in note for note in plan.notes))

    def test_no_datasets_is_an_empty_plan_not_an_error(self) -> None:
        plan = dashboard_plan.build(goal="revenue by region")
        self.assertTrue(plan.empty)
        self.assertEqual(plan.title, "Revenue by region")
        self.assertIn("no dataset or column", " ".join(plan.notes))


class GoalRelevanceTest(unittest.TestCase):
    def test_goal_keywords_rank_the_matching_dataset_first(self) -> None:
        plan = dashboard_plan.build(
            goal="student scores by course",
            datasets=[course_dataset(), student_dataset()],
            charts=[
                {"id": 41, "name": "Scores", "viz_type": "table", "dataset_id": 7},
                {"id": 42, "name": "Enrolments", "viz_type": "table", "dataset_id": 9},
            ],
        )
        self.assertEqual(plan.slots[0].dataset_id, 7)
        self.assertGreater(plan.slots[0].score, 0)

    def test_unrelated_dataset_is_excluded_with_a_reason(self) -> None:
        plan = dashboard_plan.build(
            goal="student scores by course",
            datasets=[student_dataset(), course_dataset()],
            charts=[
                {"id": 41, "name": "Scores", "viz_type": "table", "dataset_id": 7},
            ],
            max_slots=1,
        )
        reasons = {entry.dataset_id: entry.reason for entry in plan.excluded}
        self.assertEqual(reasons.get(9), "slot_budget")

    def test_audience_and_grain_are_read_from_the_goal(self) -> None:
        plan = dashboard_plan.build(
            goal="monthly executive view of student scores",
            datasets=[student_dataset()],
            candidates={7: [line_candidate()]},
            catalog=catalog(),
        )
        self.assertEqual(plan.audience, "executive")
        trend = next(slot for slot in plan.slots if slot.family == "trend")
        self.assertEqual(trend.form_data["time_grain_sqla"], "P1M")


class ReuseTest(unittest.TestCase):
    def test_existing_chart_wins_a_family_slot_before_generation(self) -> None:
        plan = dashboard_plan.build(
            goal="student scores",
            datasets=[student_dataset()],
            charts=[
                {"id": 41, "name": "Scores", "viz_type": "table", "dataset_id": 7}
            ],
            candidates={7: [pie_candidate(), line_candidate()]},
            catalog=catalog(),
        )
        detail = next(slot for slot in plan.slots if slot.family == "detail")
        self.assertEqual(detail.source, "existing")
        self.assertEqual(detail.chart_id, 41)
        self.assertEqual(plan.reuse_count, 1)

    def test_duplicate_saved_charts_of_one_family_are_collapsed(self) -> None:
        plan = dashboard_plan.build(
            goal="student scores",
            datasets=[student_dataset()],
            charts=[
                {"id": 41, "name": "Scores A", "viz_type": "table", "dataset_id": 7},
                {"id": 12, "name": "Scores B", "viz_type": "table", "dataset_id": 7},
            ],
            catalog=catalog(),
        )
        self.assertEqual(plan.reuse_count, 1)
        self.assertEqual(plan.slots[0].chart_id, 12)

    def test_generation_is_requested_when_nothing_exists(self) -> None:
        plan = dashboard_plan.build(
            goal="student scores",
            datasets=[student_dataset()],
            candidates={7: [pie_candidate(), line_candidate()]},
            catalog=catalog(),
        )
        self.assertEqual(plan.generate_count, len(plan.slots))
        self.assertEqual(plan.reuse_count, 0)
        self.assertTrue(all(slot.needs_generation for slot in plan.slots))

    def test_two_families_per_dataset_at_most(self) -> None:
        plan = dashboard_plan.build(
            goal="student scores",
            datasets=[student_dataset()],
            candidates={
                7: [
                    line_candidate(),
                    pie_candidate(),
                    {
                        "viz_type": "big_number_total",
                        "family": "single_value",
                        "title": "Total score",
                        "form_data": {"metric": "sum_score"},
                        "score": 0.3,
                    },
                ]
            },
            catalog=catalog(),
        )
        self.assertEqual(len(plan.slots), dashboard_plan.MAX_CHARTS_PER_DATASET)


class NeedTest(unittest.TestCase):
    def test_matched_dataset_without_artifacts_becomes_an_explicit_need(self) -> None:
        plan = dashboard_plan.build(
            goal="student scores",
            datasets=[student_dataset()],
            catalog=catalog(),
        )
        self.assertTrue(plan.empty)
        self.assertEqual(len(plan.needs), 1)
        self.assertEqual(plan.needs[0].dataset_id, 7)
        self.assertEqual(
            plan.needs[0].reason, "no_existing_chart_and_no_recommendation"
        )
        self.assertTrue(any("goal-matched dataset" in note for note in plan.notes))

    def test_candidate_the_deployment_cannot_draw_is_dropped(self) -> None:
        plan = dashboard_plan.build(
            goal="student scores",
            datasets=[student_dataset()],
            candidates={7: [line_candidate()]},
            catalog=catalog(available=["pie"]),
        )
        self.assertTrue(plan.empty)
        self.assertEqual(len(plan.needs), 1)

    def test_candidate_with_unsupported_controls_is_dropped(self) -> None:
        rotten = line_candidate()
        rotten["form_data"] = {"x_axis": "created_at", "pivot_rows": "course_name"}
        plan = dashboard_plan.build(
            goal="student scores",
            datasets=[student_dataset()],
            candidates={7: [rotten]},
            catalog=catalog(),
        )
        self.assertTrue(plan.empty)


class QualityTest(unittest.TestCase):
    def test_quality_measures_diversity_coverage_and_hierarchy(self) -> None:
        plan = dashboard_plan.build(
            goal="student scores",
            datasets=[student_dataset()],
            candidates={
                7: [
                    line_candidate(),
                    pie_candidate(),
                    {
                        "viz_type": "big_number_total",
                        "family": "single_value",
                        "title": "Total score",
                        "form_data": {"metric": "sum_score"},
                        "score": 0.3,
                    },
                ]
            },
            catalog=catalog(),
            max_slots=3,
        )
        quality = plan.quality
        self.assertGreater(quality["diversity"], 0.99)
        self.assertGreater(quality["coverage"], 0.99)
        self.assertEqual(quality["redundancy"], 1.0)
        self.assertEqual(quality["hierarchy"], 1.0)

    def test_sections_follow_reading_order(self) -> None:
        plan = dashboard_plan.build(
            goal="student scores",
            datasets=[student_dataset()],
            candidates={
                7: [
                    {
                        "viz_type": "big_number_total",
                        "family": "single_value",
                        "title": "Total score",
                        "form_data": {"metric": "sum_score"},
                        "score": 0.3,
                    },
                    line_candidate(),
                ]
            },
            catalog=catalog(),
        )
        self.assertEqual([section.key for section in plan.sections], ["headline", "trend"])

    def test_empty_plan_reports_zero_quality(self) -> None:
        plan = dashboard_plan.build(goal="revenue", datasets=[])
        self.assertEqual(plan.quality["diversity"], 0.0)
        self.assertEqual(plan.quality["hierarchy"], 0.0)

    def test_public_shape_is_stable(self) -> None:
        plan = dashboard_plan.build(
            goal="student scores",
            datasets=[student_dataset()],
            candidates={7: [line_candidate()]},
            catalog=catalog(),
        )
        body = plan.to_public_dict()
        self.assertEqual(body["count"], len(plan.slots))
        self.assertEqual(body["generate_count"], 1)
        self.assertEqual(body["slots"][0]["source"], "generate")
        self.assertIn("quality", body)
        self.assertIsInstance(body["notes"], list)


if __name__ == "__main__":
    unittest.main()
