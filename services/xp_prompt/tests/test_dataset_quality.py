"""Tests for the dataset quality rules (P6-F04)."""

from __future__ import annotations

import unittest

from xp_prompt import dataset_quality, dataset_spec, schema_slice


def slice_() -> schema_slice.SchemaSlice:
    return schema_slice.SchemaSlice.from_payload(
        {
            "datasets": [
                {
                    "name": "mdl_user",
                    "schema": "public",
                    "database": "lms",
                    "dialect": "postgres",
                    "columns": [
                        {"name": "id", "type": "bigint"},
                        {"name": "firstname", "type": "text"},
                        {"name": "created_at", "type": "timestamp"},
                        {"name": "score", "type": "numeric"},
                    ],
                }
            ]
        }
    )


def spec(**overrides) -> dataset_spec.DatasetSpec:
    body = {
        "name": "users",
        "kind": "virtual",
        "database": "lms",
        "sql": "select id, firstname, created_at, score from public.mdl_user",
        "columns": [
            {"name": "id", "type": "bigint"},
            {"name": "firstname", "type": "text"},
            {"name": "created_at", "type": "timestamp"},
            {"name": "score", "type": "numeric"},
        ],
        "grain": ["id"],
        "metrics": [{"name": "Users", "expression": "COUNT(*)"}],
    }
    body.update(overrides)
    return dataset_spec.from_payload(body, schema=slice_())


def profile(**overrides):
    body = {
        "row_count": 100,
        "sampled": True,
        "duplicate_grain": 0,
        "columns": [
            {"name": "id", "null_count": 0, "distinct_count": 100, "observed_type": "bigint"},
            {
                "name": "firstname",
                "null_count": 2,
                "distinct_count": 90,
                "observed_type": "text",
            },
            {
                "name": "created_at",
                "null_count": 0,
                "distinct_count": 80,
                "observed_type": "timestamp",
                "invalid_dates": 0,
            },
            {"name": "score", "null_count": 1, "distinct_count": 40, "observed_type": "numeric"},
        ],
    }
    body.update(overrides)
    return body


def codes(report: dataset_quality.QualityReport) -> list[str]:
    return [warning.code for warning in report.warnings]


class CleanProfileTest(unittest.TestCase):
    def test_a_clean_sample_passes(self) -> None:
        report = dataset_quality.check(spec(), profile())
        self.assertTrue(report.ok)
        self.assertFalse(report.blocking)
        self.assertEqual(report.warnings, ())
        self.assertEqual(report.row_count, 100)

    def test_the_report_is_serialisable_and_carries_no_rows(self) -> None:
        payload = dataset_quality.check(spec(), profile()).to_public_dict()
        self.assertIn("codes", payload)
        self.assertNotIn("rows", payload)


class EmptyTest(unittest.TestCase):
    def test_an_empty_definition_blocks(self) -> None:
        report = dataset_quality.check(spec(), profile(row_count=0))
        self.assertFalse(report.ok)
        self.assertTrue(report.blocking)
        self.assertIn("empty_result", codes(report))

    def test_an_unmeasured_row_count_is_unknown_not_zero(self) -> None:
        report = dataset_quality.check(spec(), {"columns": []})
        self.assertNotIn("empty_result", codes(report))
        self.assertEqual(report.row_count, 0)


class GrainTest(unittest.TestCase):
    def test_a_duplicated_grain_blocks(self) -> None:
        report = dataset_quality.check(spec(), profile(duplicate_grain=10))
        self.assertTrue(report.blocking)
        self.assertIn("duplicate_grain", codes(report))

    def test_a_negligible_duplicate_share_is_tolerated(self) -> None:
        report = dataset_quality.check(spec(), profile(row_count=10_000, duplicate_grain=5))
        self.assertNotIn("duplicate_grain", codes(report))

    def test_nulls_in_the_grain_block(self) -> None:
        report = dataset_quality.check(
            spec(),
            profile(
                columns=[
                    {"name": "id", "null_count": 3, "distinct_count": 97},
                ]
            ),
        )
        self.assertTrue(report.blocking)
        self.assertIn("null_in_grain", codes(report))

    def test_no_grain_is_a_warning(self) -> None:
        report = dataset_quality.check(spec(grain=[]), profile())
        self.assertFalse(report.blocking)
        self.assertIn("no_grain", codes(report))

    def test_a_grain_outside_the_columns_blocks(self) -> None:
        report = dataset_quality.check(spec(grain=["id"]), profile())
        self.assertNotIn("grain_not_in_columns", codes(report))


class ColumnTest(unittest.TestCase):
    def test_a_mostly_null_column_blocks(self) -> None:
        report = dataset_quality.check(
            spec(),
            profile(
                columns=[
                    {"name": "firstname", "null_count": 95, "distinct_count": 5},
                ]
            ),
        )
        self.assertTrue(report.blocking)
        self.assertIn("column_mostly_null", codes(report))

    def test_a_null_heavy_column_warns(self) -> None:
        report = dataset_quality.check(
            spec(),
            profile(
                columns=[
                    {"name": "firstname", "null_count": 30, "distinct_count": 50},
                ]
            ),
        )
        self.assertFalse(report.blocking)
        self.assertIn("column_null_heavy", codes(report))

    def test_a_constant_column_warns(self) -> None:
        report = dataset_quality.check(
            spec(),
            profile(columns=[{"name": "score", "distinct_count": 1, "null_count": 0}]),
        )
        self.assertIn("column_constant", codes(report))

    def test_type_drift_warns(self) -> None:
        report = dataset_quality.check(
            spec(),
            profile(columns=[{"name": "score", "observed_type": "varchar"}]),
        )
        self.assertIn("type_drift", codes(report))

    def test_invalid_dates_on_the_temporal_column_block(self) -> None:
        report = dataset_quality.check(
            spec(),
            profile(columns=[{"name": "created_at", "invalid_dates": 4}]),
        )
        self.assertTrue(report.blocking)
        self.assertIn("invalid_dates", codes(report))

    def test_invalid_dates_elsewhere_only_warn(self) -> None:
        report = dataset_quality.check(
            spec(),
            profile(columns=[{"name": "score", "invalid_dates": 4}]),
        )
        self.assertFalse(report.blocking)
        self.assertIn("invalid_dates", codes(report))

    def test_an_unmeasured_column_is_skipped_not_passed(self) -> None:
        report = dataset_quality.check(
            spec(), profile(columns=[])
        )
        self.assertEqual(report.checked_columns, 4)
        self.assertNotIn("column_constant", codes(report))


class MetricTest(unittest.TestCase):
    def test_a_metric_on_a_text_column_warns(self) -> None:
        report = dataset_quality.check(
            spec(metrics=[{"name": "Name total", "expression": "SUM(firstname)"}]),
            profile(),
        )
        self.assertIn("metric_not_numeric", codes(report))

    def test_counting_is_always_plausible(self) -> None:
        report = dataset_quality.check(
            spec(metrics=[{"name": "Rows", "expression": "COUNT(firstname)"}]),
            profile(),
        )
        self.assertNotIn("metric_not_numeric", codes(report))

    def test_a_metric_on_a_numeric_column_passes(self) -> None:
        report = dataset_quality.check(
            spec(metrics=[{"name": "Total score", "expression": "SUM(score)"}]),
            profile(),
        )
        self.assertNotIn("metric_not_numeric", codes(report))


class ConfirmationTest(unittest.TestCase):
    def test_warnings_require_confirmation_without_blocking(self) -> None:
        report = dataset_quality.check(spec(grain=[]), profile())
        self.assertFalse(report.blocking)
        self.assertTrue(report.requires_confirmation)

    def test_the_warning_list_is_bounded(self) -> None:
        many = [
            {"name": f"col_{index}", "null_count": 95, "distinct_count": 2}
            for index in range(60)
        ]
        spec_ = spec(columns=[{"name": f"col_{index}", "type": "text"} for index in range(60)])
        report = dataset_quality.check(spec_, profile(columns=many))
        self.assertLessEqual(len(report.warnings), dataset_quality.MAX_WARNINGS)


if __name__ == "__main__":
    unittest.main()
