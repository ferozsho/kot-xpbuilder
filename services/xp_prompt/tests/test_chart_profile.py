"""Tests for the field profile (P7-F02)."""

from __future__ import annotations

import unittest

from xp_prompt import chart_profile


def columns():
    return [
        {"name": "id", "type": "bigint"},
        {"name": "name", "type": "text"},
        {"name": "created_at", "type": "timestamp"},
        {"name": "score", "type": "numeric"},
        {"name": "country", "type": "text"},
        {"name": "is_active", "type": "boolean"},
    ]


def measured(**overrides):
    entries = {
        "id": {"distinct_count": 900, "null_count": 0},
        "name": {"distinct_count": 500, "null_count": 2},
        "created_at": {"distinct_count": 120, "null_count": 0, "observed_type": "timestamp"},
        "score": {"distinct_count": 40, "null_count": 1, "observed_type": "numeric"},
        "country": {"distinct_count": 4, "null_count": 0, "observed_type": "text"},
        "is_active": {"distinct_count": 2, "null_count": 0, "observed_type": "boolean"},
    }
    entries.update(overrides)
    return [
        {"name": name, **stats} for name, stats in entries.items()
    ]


def profile(**kwargs) -> chart_profile.DatasetProfile:
    return chart_profile.profile(
        kwargs.pop("columns", columns()),
        measured=kwargs.pop("measured", measured()),
        row_count=kwargs.pop("row_count", 1_000),
        **kwargs,
    )


class KindTest(unittest.TestCase):
    def test_declared_types_decide_the_kind(self) -> None:
        found = profile()
        by_name = {field.name: field for field in found.fields}

        self.assertEqual(by_name["created_at"].kind, chart_profile.TEMPORAL)
        self.assertEqual(by_name["score"].kind, chart_profile.NUMERIC)
        self.assertEqual(by_name["is_active"].kind, chart_profile.BOOLEAN)

    def test_an_identifier_is_not_a_measure(self) -> None:
        found = profile()
        field = next(item for item in found.fields if item.name == "id")

        self.assertEqual(field.kind, chart_profile.CATEGORICAL)
        self.assertTrue(field.is_identifier)
        self.assertNotEqual(found.measure().name, "id")

    def test_a_measured_type_overrides_a_declared_one(self) -> None:
        found = profile(
            measured=measured(score={"distinct_count": 5, "observed_type": "text"})
        )
        field = next(item for item in found.fields if item.name == "score")

        self.assertEqual(field.kind, chart_profile.CATEGORICAL)

    def test_geography_is_a_hint_not_a_kind(self) -> None:
        field = next(item for item in profile().fields if item.name == "country")

        self.assertTrue(field.is_geographic)
        self.assertEqual(field.kind, chart_profile.CATEGORICAL)


class AggregateTest(unittest.TestCase):
    def test_the_temporal_axis_is_the_declared_temporal_column(self) -> None:
        self.assertEqual(profile().temporal().name, "created_at")

    def test_there_is_no_temporal_axis_without_one(self) -> None:
        found = profile(columns=[{"name": "score", "type": "numeric"}], measured=[])
        self.assertIsNone(found.temporal())

    def test_the_measure_is_a_non_identifier_numeric(self) -> None:
        self.assertEqual(profile().measure().name, "score")

    def test_categoricals_exclude_identifiers(self) -> None:
        names = [field.name for field in profile().categoricals]

        self.assertNotIn("id", names)
        self.assertIn("country", names)

    def test_low_cardinality_is_a_measured_fact(self) -> None:
        found = profile()
        country = next(item for item in found.fields if item.name == "country")
        unknown = chart_profile.Field(name="x", type="text", kind="categorical")

        self.assertTrue(found.is_low_cardinality(country))
        self.assertFalse(found.is_low_cardinality(unknown))

    def test_cardinality_and_null_share_are_recorded(self) -> None:
        field = next(item for item in profile().fields if item.name == "score")

        self.assertEqual(field.cardinality, 40)
        self.assertAlmostEqual(field.null_share, 0.001)


class FingerprintTest(unittest.TestCase):
    def test_the_same_shape_has_the_same_fingerprint(self) -> None:
        self.assertEqual(profile().fingerprint, profile().fingerprint)

    def test_a_different_shape_has_a_different_fingerprint(self) -> None:
        other = profile(columns=[{"name": "score", "type": "numeric"}], measured=[])

        self.assertNotEqual(profile().fingerprint, other.fingerprint)

    def test_the_fingerprint_carries_no_names_of_values(self) -> None:
        self.assertEqual(len(profile().fingerprint), 64)


class EdgeTest(unittest.TestCase):
    def test_duplicate_columns_collapse(self) -> None:
        found = profile(
            columns=[{"name": "score", "type": "numeric"}, {"name": "SCORE", "type": "numeric"}],
            measured=[],
        )

        self.assertEqual(len(found.fields), 1)

    def test_a_missing_name_is_skipped(self) -> None:
        found = profile(columns=[{"name": "", "type": "text"}], measured=[])

        self.assertEqual(found.fields, ())

    def test_an_unmeasured_dataset_still_profiles(self) -> None:
        found = profile(measured=[], row_count=0)
        field = next(item for item in found.fields if item.name == "country")

        self.assertIsNone(field.cardinality)
        self.assertIsNone(field.null_share)

    def test_the_public_shape_is_serialisable(self) -> None:
        payload = profile().to_public_dict()

        self.assertIn("fields", payload)
        self.assertEqual(payload["row_count"], 1_000)


if __name__ == "__main__":
    unittest.main()
