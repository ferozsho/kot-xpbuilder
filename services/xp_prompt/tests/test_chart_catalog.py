"""Tests for the visualization catalog (P7-F01)."""

from __future__ import annotations

import unittest

from xp_prompt import chart_catalog

MODERN = [
    {"viz_type": "echarts_timeseries_line", "name": "Line chart"},
    {"viz_type": "echarts_timeseries_bar", "name": "Bar chart"},
    {"viz_type": "echarts_timeseries_area"},
    {"viz_type": "echarts_timeseries_scatter"},
    {"viz_type": "pie"},
    {"viz_type": "heatmap_v2"},
    {"viz_type": "big_number_total"},
    {"viz_type": "table"},
]


def catalog(**overrides) -> chart_catalog.Catalog:
    payload = {"viz_types": MODERN, "denylist": []}
    payload.update(overrides)
    return chart_catalog.Catalog.from_payload(payload)


class AvailabilityTest(unittest.TestCase):
    def test_only_reported_types_are_available(self) -> None:
        found = catalog()

        self.assertTrue(found.has("pie"))
        self.assertFalse(found.has("echarts_timeseries_line_no_such"))

    def test_an_empty_report_means_nothing_is_available(self) -> None:
        empty = chart_catalog.Catalog.from_payload({})

        self.assertEqual(empty.available, frozenset())
        self.assertEqual(empty.supported(), [])
        self.assertFalse(empty.has("table"))

    def test_a_denied_type_is_not_available_even_if_reported(self) -> None:
        found = catalog(denylist=["pie"])

        self.assertFalse(found.has("pie"))
        self.assertIn("pie", found.denied)

    def test_the_catalog_accepts_plain_strings_too(self) -> None:
        found = chart_catalog.Catalog.from_payload({"viz_types": ["table", "PIE"]})

        self.assertTrue(found.has("table"))
        self.assertTrue(found.has("PIE"))

    def test_a_viz_name_is_normalised(self) -> None:
        found = chart_catalog.Catalog.from_payload({"viz_types": ["ECharts-Pie"]})

        self.assertTrue(found.has("echarts_pie"))

    def test_the_catalog_is_bounded(self) -> None:
        many = [f"viz_{index}" for index in range(500)]
        found = chart_catalog.Catalog.from_payload({"viz_types": many})

        self.assertLessEqual(len(found.available), chart_catalog.MAX_VIZ_TYPES)


class ContractTest(unittest.TestCase):
    def test_a_contract_is_only_offered_when_available(self) -> None:
        found = chart_catalog.Catalog.from_payload({"viz_types": ["table"]})

        self.assertIsNotNone(found.contract("table"))
        self.assertIsNone(found.contract("pie"))

    def test_a_line_chart_needs_an_axis_and_a_metric(self) -> None:
        contract = chart_catalog.VIZ_CONTRACTS["echarts_timeseries_line"]

        ok, problems = contract.accepts({"metrics": ["x"]})
        self.assertFalse(ok)
        self.assertIn("missing:x_axis", problems)

        ok, problems = contract.accepts({"x_axis": "d", "metrics": ["x"]})
        self.assertTrue(ok)
        self.assertEqual(problems, ())

    def test_an_unsupported_control_is_refused(self) -> None:
        ok, problems = chart_catalog.VIZ_CONTRACTS["pie"].accepts(
            {"groupby": ["c"], "metric": "m", "series_limit": 5}
        )

        self.assertFalse(ok)
        self.assertIn("series_limit", problems)

    def test_common_keys_are_accepted_everywhere(self) -> None:
        for contract in chart_catalog.VIZ_CONTRACTS.values():
            self.assertTrue(chart_catalog.COMMON_KEYS <= contract.allows, contract.viz_type)

    def test_validate_reports_an_unavailable_type(self) -> None:
        ok, problems = chart_catalog.Catalog.from_payload({}).validate("pie", {})

        self.assertFalse(ok)
        self.assertIn("unavailable_viz_type", problems)

    def test_validate_refuses_an_oversized_form(self) -> None:
        form = {"x_axis": "d", "metrics": ["m"]}
        form.update({f"extra_{index}": 1 for index in range(chart_catalog.MAX_FORM_KEYS)})

        ok, problems = catalog().validate("echarts_timeseries_line", form)

        self.assertFalse(ok)
        self.assertIn("too_many_controls", problems)

    def test_the_public_shape_lists_contracts_only_for_available_types(self) -> None:
        payload = catalog().to_public_dict()
        listed = {entry["viz_type"] for entry in payload["contracts"]}

        self.assertEqual(listed, set(chart_catalog.VIZ_CONTRACTS))
        self.assertEqual(payload["available"], sorted(catalog().available))


class CoverageTest(unittest.TestCase):
    def test_every_contract_names_a_viz_type_key(self) -> None:
        for key, contract in chart_catalog.VIZ_CONTRACTS.items():
            self.assertEqual(key, contract.viz_type)

    def test_every_contract_allows_the_viz_type_key(self) -> None:
        for contract in chart_catalog.VIZ_CONTRACTS.values():
            self.assertIn("viz_type", contract.allows, contract.viz_type)

    def test_requires_are_a_subset_of_allows(self) -> None:
        for contract in chart_catalog.VIZ_CONTRACTS.values():
            self.assertTrue(contract.requires <= contract.allows, contract.viz_type)


if __name__ == "__main__":
    unittest.main()
