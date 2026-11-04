"""Tests for the chart rule engine (P7-F03)."""

from __future__ import annotations

import unittest

from xp_prompt import chart_catalog, chart_profile, chart_rules

ALL_VIZ = [
    "echarts_timeseries_line",
    "echarts_timeseries_bar",
    "echarts_timeseries_area",
    "echarts_timeseries_scatter",
    "pie",
    "heatmap_v2",
    "big_number_total",
    "table",
]


def catalog(denylist=None) -> chart_catalog.Catalog:
    return chart_catalog.Catalog.from_payload(
        {"viz_types": ALL_VIZ, "denylist": denylist or []}
    )


def profile(columns, measured=None, row_count=1_000, grain=None):
    return chart_profile.profile(
        columns, measured=measured or [], row_count=row_count, grain=grain
    )


TIME_MEASURE = profile(
    [
        {"name": "created_at", "type": "timestamp"},
        {"name": "score", "type": "numeric"},
    ],
    measured=[
        {"name": "created_at", "distinct_count": 24},
        {"name": "score", "distinct_count": 40},
    ],
)

CATEGORY_MEASURE = profile(
    [
        {"name": "country", "type": "text"},
        {"name": "score", "type": "numeric"},
    ],
    measured=[
        {"name": "country", "distinct_count": 4},
        {"name": "score", "distinct_count": 40},
    ],
)


def viz_types(recommendation: chart_rules.Recommendation) -> list[str]:
    return [candidate.viz_type for candidate in recommendation.candidates]


class TrendTest(unittest.TestCase):
    def test_a_time_series_with_a_measure_recommends_a_line_first(self) -> None:
        found = chart_rules.recommend(TIME_MEASURE, catalog())

        self.assertEqual(found.candidates[0].viz_type, "echarts_timeseries_line")
        self.assertEqual(found.candidates[0].form_data["x_axis"], "created_at")
        self.assertEqual(found.candidates[0].form_data["metrics"], ["score"])

    def test_the_rationale_names_the_fields_it_used(self) -> None:
        found = chart_rules.recommend(TIME_MEASURE, catalog())

        self.assertIn("created_at", found.candidates[0].rationale)
        self.assertIn("score", found.candidates[0].rationale)

    def test_two_periods_are_not_a_trend(self) -> None:
        sparse = profile(
            [{"name": "created_at", "type": "timestamp"}, {"name": "score", "type": "numeric"}],
            measured=[
                {"name": "created_at", "distinct_count": 2},
                {"name": "score", "distinct_count": 5},
            ],
        )
        found = chart_rules.recommend(sparse, catalog())

        self.assertNotIn("echarts_timeseries_line", viz_types(found))
        self.assertIn(
            "too_few_periods", [entry.reason for entry in found.eliminated]
        )

    def test_too_many_periods_are_eliminated_rather_than_drawn(self) -> None:
        dense = profile(
            [{"name": "created_at", "type": "timestamp"}, {"name": "score", "type": "numeric"}],
            measured=[
                {"name": "created_at", "distinct_count": 5_000},
                {"name": "score", "distinct_count": 5},
            ],
        )
        found = chart_rules.recommend(dense, catalog())

        self.assertNotIn("echarts_timeseries_line", viz_types(found))
        self.assertIn("too_many_periods", [entry.reason for entry in found.eliminated])

    def test_a_cumulative_measure_also_offers_an_area(self) -> None:
        cumulative = profile(
            [
                {"name": "created_at", "type": "timestamp"},
                {"name": "total_score", "type": "numeric"},
            ],
            measured=[
                {"name": "created_at", "distinct_count": 24},
                {"name": "total_score", "distinct_count": 40},
            ],
        )
        found = chart_rules.recommend(cumulative, catalog())

        self.assertIn("echarts_timeseries_area", viz_types(found))


class ComparisonTest(unittest.TestCase):
    def test_a_low_cardinality_category_with_a_measure_recommends_a_bar(self) -> None:
        found = chart_rules.recommend(CATEGORY_MEASURE, catalog())

        self.assertIn("echarts_timeseries_bar", viz_types(found))
        bar = next(item for item in found.candidates if item.viz_type == "echarts_timeseries_bar")
        self.assertEqual(bar.form_data["x_axis"], "country")

    def test_without_a_category_a_bar_is_eliminated(self) -> None:
        found = chart_rules.recommend(TIME_MEASURE, catalog())

        self.assertIn("no_category", [entry.reason for entry in found.eliminated])

    def test_a_high_cardinality_category_is_not_a_bar_axis(self) -> None:
        wide = profile(
            [{"name": "student", "type": "text"}, {"name": "score", "type": "numeric"}],
            measured=[
                {"name": "student", "distinct_count": 5_000},
                {"name": "score", "distinct_count": 40},
            ],
        )
        found = chart_rules.recommend(wide, catalog())

        self.assertNotIn("echarts_timeseries_bar", viz_types(found))
        self.assertIn(
            "category_too_high_cardinality",
            [entry.reason for entry in found.eliminated],
        )


class PartToWholeTest(unittest.TestCase):
    def test_a_small_category_offers_a_pie(self) -> None:
        found = chart_rules.recommend(CATEGORY_MEASURE, catalog())

        self.assertIn("pie", viz_types(found))

    def test_too_many_slices_are_refused(self) -> None:
        many = profile(
            [{"name": "country", "type": "text"}, {"name": "score", "type": "numeric"}],
            measured=[
                {"name": "country", "distinct_count": 9},
                {"name": "score", "distinct_count": 40},
            ],
        )
        found = chart_rules.recommend(many, catalog())

        self.assertNotIn("pie", viz_types(found))
        self.assertIn("too_many_slices", [entry.reason for entry in found.eliminated])

    def test_an_unmeasured_cardinality_does_not_earn_a_pie(self) -> None:
        unknown = profile(
            [{"name": "country", "type": "text"}, {"name": "score", "type": "numeric"}],
            measured=[],
        )
        found = chart_rules.recommend(unknown, catalog())

        self.assertNotIn("pie", viz_types(found))
        self.assertIn("cardinality_unknown", [entry.reason for entry in found.eliminated])


class MatrixAndCorrelationTest(unittest.TestCase):
    def test_two_small_categories_offer_a_heatmap(self) -> None:
        two = profile(
            [
                {"name": "grade", "type": "text"},
                {"name": "term", "type": "text"},
                {"name": "score", "type": "numeric"},
            ],
            measured=[
                {"name": "grade", "distinct_count": 5},
                {"name": "term", "distinct_count": 3},
                {"name": "score", "distinct_count": 40},
            ],
        )
        found = chart_rules.recommend(two, catalog())

        self.assertIn("heatmap_v2", viz_types(found))

    def test_two_measures_offer_a_scatter(self) -> None:
        two = profile(
            [{"name": "score", "type": "numeric"}, {"name": "minutes", "type": "numeric"}],
            measured=[
                {"name": "score", "distinct_count": 40},
                {"name": "minutes", "distinct_count": 300},
            ],
        )
        found = chart_rules.recommend(two, catalog())

        self.assertIn("echarts_timeseries_scatter", viz_types(found))


class SingleValueAndDetailTest(unittest.TestCase):
    def test_one_measure_offers_a_big_number(self) -> None:
        single = profile(
            [{"name": "score", "type": "numeric"}],
            measured=[{"name": "score", "distinct_count": 40}],
        )
        found = chart_rules.recommend(single, catalog())

        self.assertIn("big_number_total", viz_types(found))

    def test_a_wide_category_makes_the_table_the_best_answer(self) -> None:
        wide = profile(
            [{"name": "student", "type": "text"}, {"name": "score", "type": "numeric"}],
            measured=[
                {"name": "student", "distinct_count": 5_000},
                {"name": "score", "distinct_count": 40},
            ],
        )
        found = chart_rules.recommend(wide, catalog())

        self.assertEqual(found.candidates[0].viz_type, "table")
        self.assertGreater(found.candidates[0].score, 0.5)

    def test_the_table_is_always_offered(self) -> None:
        for sample in (TIME_MEASURE, CATEGORY_MEASURE):
            self.assertIn("table", viz_types(chart_rules.recommend(sample, catalog())))


class CatalogGateTest(unittest.TestCase):
    def test_a_denied_viz_type_is_never_recommended(self) -> None:
        found = chart_rules.recommend(TIME_MEASURE, catalog(denylist=["echarts_timeseries_line"]))

        self.assertNotIn("echarts_timeseries_line", viz_types(found))
        self.assertIn(
            "unavailable_viz_type", [entry.reason for entry in found.eliminated]
        )

    def test_an_empty_catalog_recommends_nothing(self) -> None:
        empty = chart_catalog.Catalog.from_payload({})
        found = chart_rules.recommend(TIME_MEASURE, empty)

        self.assertTrue(found.empty)
        self.assertEqual(found.candidates, ())

    def test_every_candidate_carries_its_viz_type_in_the_form(self) -> None:
        found = chart_rules.recommend(TIME_MEASURE, catalog())

        for candidate in found.candidates:
            self.assertEqual(candidate.form_data["viz_type"], candidate.viz_type)

    def test_every_candidate_passes_its_own_contract(self) -> None:
        found = chart_rules.recommend(CATEGORY_MEASURE, catalog())

        for candidate in found.candidates:
            ok, problems = catalog().validate(candidate.viz_type, candidate.form_data)
            self.assertTrue(ok, f"{candidate.viz_type}: {problems}")

    def test_a_profile_without_fields_recommends_nothing(self) -> None:
        found = chart_rules.recommend(profile([], measured=[]), catalog())

        self.assertTrue(found.empty)


class ShapingTest(unittest.TestCase):
    def test_candidates_are_bounded(self) -> None:
        found = chart_rules.recommend(CATEGORY_MEASURE, catalog())

        self.assertLessEqual(len(found.candidates), chart_rules.MAX_CANDIDATES + 1)

    def test_the_table_survives_the_candidate_cap(self) -> None:
        # A rich profile produces more candidates than the cap; the fallback must stay.
        rich = profile(
            [
                {"name": "submitted_at", "type": "timestamp"},
                {"name": "grade", "type": "text"},
                {"name": "term", "type": "text"},
                {"name": "score", "type": "numeric"},
                {"name": "minutes", "type": "numeric"},
            ],
            measured=[
                {"name": "submitted_at", "distinct_count": 24},
                {"name": "grade", "distinct_count": 5},
                {"name": "term", "distinct_count": 3},
                {"name": "score", "distinct_count": 40},
                {"name": "minutes", "distinct_count": 300},
            ],
        )
        found = chart_rules.recommend(rich, catalog())

        self.assertIn("table", viz_types(found))

    def test_candidates_are_ordered_by_score(self) -> None:
        found = chart_rules.recommend(CATEGORY_MEASURE, catalog())
        scores = [candidate.score for candidate in found.candidates]

        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_the_public_shape_is_serialisable(self) -> None:
        payload = chart_rules.recommend(TIME_MEASURE, catalog()).to_public_dict()

        self.assertIn("candidates", payload)
        self.assertIn("eliminated", payload)
        self.assertEqual(payload["count"], len(payload["candidates"]))


if __name__ == "__main__":
    unittest.main()
