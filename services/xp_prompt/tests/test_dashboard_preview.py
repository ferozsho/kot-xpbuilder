# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Tests for complete, read-only dashboard preview metadata (P8-F06/F07)."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from xp_prompt import dashboard_preview


def slot(slot_id: str, dataset_id: int = 7, **extra) -> dict:
    body = {
        "id": slot_id,
        "title": f"Chart {slot_id}",
        "question": "How are scores changing?",
        "family": "trend",
        "section": "trend",
        "dataset_id": dataset_id,
        "dataset_name": f"dataset_{dataset_id}",
        "viz_type": "echarts_timeseries_line",
        "chart_id": 40 + dataset_id,
        "source": "existing",
    }
    body.update(extra)
    return body


def render(slot_id: str, **extra) -> dict:
    body = {
        "slot_id": slot_id,
        "columns": ["month", "score"],
        "rows": [["2026-10", 82], ["2026-11", 85]],
        "row_count": 2,
        "truncated": False,
    }
    body.update(extra)
    return body


class DashboardPreviewTest(unittest.TestCase):
    def test_complete_preview_has_generated_metadata_and_no_persistence(self) -> None:
        preview = dashboard_preview.build(
            title="Student performance",
            goal="Track student scores by month",
            audience="executive",
            slots=[slot("7:trend"), slot("9:detail", 9, family="detail")],
            renders=[render("7:trend"), render("9:detail")],
            generated_at=datetime(2026, 11, 4, 13, 30, tzinfo=UTC),
        ).to_public_dict()

        self.assertTrue(preview["complete"])
        self.assertFalse(preview["persisted"])
        self.assertTrue(preview["publish_required"])
        self.assertEqual(preview["metadata"]["title"], "Student performance")
        self.assertEqual(preview["metadata"]["generated_at"], "2026-11-04T13:30:00Z")
        self.assertEqual(
            preview["metadata"]["last_generated_at"], "2026-11-04T13:30:00Z"
        )
        self.assertEqual(preview["metadata"]["run_count"], 2)
        self.assertEqual(preview["metadata"]["dataset_count"], 2)
        self.assertIn("executive audience", preview["metadata"]["description"])

    def test_plan_order_wins_even_when_renders_arrive_out_of_order(self) -> None:
        preview = dashboard_preview.build(
            slots=[slot("first"), slot("second", 9)],
            renders=[render("second"), render("first")],
        )
        self.assertEqual([entry["id"] for entry in preview.slots], ["first", "second"])

    def test_each_slot_has_accessible_context_and_one_run(self) -> None:
        entry = dashboard_preview.build(
            slots=[slot("7:trend")], renders=[render("7:trend")]
        ).slots[0]
        self.assertIn(
            "answering: How are scores changing?", entry["accessible_context"]
        )
        self.assertIn("Showing 2 of 2 row(s)", entry["description"])
        self.assertEqual(entry["run_count"], 1)

    def test_sensitive_result_columns_are_masked(self) -> None:
        preview = dashboard_preview.build(
            slots=[slot("safe")],
            renders=[
                render(
                    "safe",
                    columns=["student", "token"],
                    rows=[["Ada", "should-not-leave-the-service"]],
                    row_count=1,
                )
            ],
        )
        data = preview.slots[0]["data"]
        self.assertEqual(data["rows"], [["Ada", "[redacted]"]])
        self.assertIn("masked_columns", data["issues"])

    def test_missing_render_is_refused(self) -> None:
        with self.assertRaisesRegex(
            dashboard_preview.DashboardPreviewError, "no chart-data result"
        ) as caught:
            dashboard_preview.build(slots=[slot("missing")], renders=[])
        self.assertEqual(caught.exception.code, "missing_render")

    def test_unexpected_render_is_refused(self) -> None:
        with self.assertRaises(dashboard_preview.DashboardPreviewError) as caught:
            dashboard_preview.build(
                slots=[slot("expected")],
                renders=[render("expected"), render("extra")],
            )
        self.assertEqual(caught.exception.code, "unexpected_render")

    def test_duplicate_slot_and_render_are_refused(self) -> None:
        with self.assertRaises(dashboard_preview.DashboardPreviewError) as slot_error:
            dashboard_preview.build(
                slots=[slot("same"), slot("same")], renders=[render("same")]
            )
        self.assertEqual(slot_error.exception.code, "duplicate_slot")

        with self.assertRaises(dashboard_preview.DashboardPreviewError) as render_error:
            dashboard_preview.build(
                slots=[slot("same")], renders=[render("same"), render("same")]
            )
        self.assertEqual(render_error.exception.code, "duplicate_render")

    def test_empty_and_over_budget_previews_are_refused(self) -> None:
        with self.assertRaises(dashboard_preview.DashboardPreviewError) as empty:
            dashboard_preview.build(slots=[], renders=[])
        self.assertEqual(empty.exception.code, "empty_preview")

        too_many = [slot(f"slot-{index}") for index in range(9)]
        with self.assertRaises(dashboard_preview.DashboardPreviewError) as budget:
            dashboard_preview.build(slots=too_many, renders=[])
        self.assertEqual(budget.exception.code, "slot_budget")

    def test_result_without_columns_cannot_make_a_complete_preview(self) -> None:
        with self.assertRaises(dashboard_preview.DashboardPreviewError) as caught:
            dashboard_preview.build(
                slots=[slot("broken")],
                renders=[render("broken", columns=[], rows=[])],
            )
        self.assertEqual(caught.exception.code, "invalid_render")


if __name__ == "__main__":
    unittest.main()
