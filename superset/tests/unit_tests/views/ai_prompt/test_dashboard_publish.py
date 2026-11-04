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
"""Unit tests for tracked, compensable dashboard publishing (P8-F08..F11).

The interesting behaviour is what happens when a publish does *not* finish, so
these tests drive the failure path rather than the happy one: the unwind order,
the difference between deleting a dashboard this attempt made and restoring one
it edited, and the outcome recorded for an object that could not be undone.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest import mock

POSITION = {
    "DASHBOARD_VERSION_KEY": "v2",
    "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": []},
}


class _Recording:
    """A command double that records how it was called and returns a fixed result."""

    def __init__(self, calls: list, name: str, result: Any = None) -> None:
        self.calls = calls
        self.name = name
        self.result = result

    def __call__(self, *args: Any) -> "_Recording":
        self.calls.append((self.name, args))
        return self

    def run(self) -> Any:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Boom(_Recording):
    def run(self) -> Any:
        raise RuntimeError("superset refused")


def _dashboard(
    identifier: int = 5,
    title: str = "Original",
    position: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=identifier,
        dashboard_title=title,
        position_json=position if position is not None else json.dumps(POSITION),
        json_metadata=json.dumps({"native_filters": []}),
        owners=[SimpleNamespace(id=1)],
        roles=[SimpleNamespace(id=2)],
        slug="original",
    )


def _publisher(module: Any, calls: list, chart_ids: list[int] | None = None):
    ids = list(chart_ids or [7, 8])

    def factory(_owner: int, _dataset: Any, draft: dict) -> SimpleNamespace:
        chart_id = ids.pop(0)
        calls.append(("create_chart", (chart_id, draft.get("title"))))
        return SimpleNamespace(id=chart_id)

    return module.DashboardPublisher(1, chart_factory=factory)


def test_a_created_chart_is_recorded_before_the_next_step(app_context: None) -> None:
    from superset.views.ai_prompt import dashboard_publish as publish

    calls: list = []
    publisher = _publisher(publish, calls)

    publisher.ensure_chart(SimpleNamespace(id=3), {"title": "Admissions", "viz_type": "pie"})

    assert publisher.created_objects() == [
        {
            "kind": "chart",
            "id": 7,
            "action": "create",
            "outcome": "",
            "detail": "Admissions",
        }
    ]


def test_compensation_deletes_the_dashboard_before_its_charts(
    app_context: None,
) -> None:
    from superset.views.ai_prompt import dashboard_publish as publish

    calls: list = []
    publisher = _publisher(publish, calls)
    publisher.ensure_chart(SimpleNamespace(id=3), {"title": "One"})
    publisher.ensure_chart(SimpleNamespace(id=4), {"title": "Two"})

    with (
        mock.patch.object(
            publish,
            "CreateDashboardCommand",
            _Recording(calls, "create_dashboard", SimpleNamespace(id=90)),
        ),
        mock.patch.object(publish, "DeleteDashboardCommand", _Recording(calls, "drop_dashboard")),
        mock.patch.object(publish, "DeleteChartCommand", _Recording(calls, "drop_chart")),
    ):
        publisher.create_dashboard({"dashboard_title": "New"})
        outcomes = publisher.compensate()

    order = [name for name, _ in calls if name in {"drop_dashboard", "drop_chart"}]
    # Charts cannot be deleted while a dashboard references them.
    assert order == ["drop_dashboard", "drop_chart", "drop_chart"]
    # The chart created last is removed first.
    assert [outcome["id"] for outcome in outcomes] == [90, 8, 7]
    assert all(outcome["outcome"] == "deleted" for outcome in outcomes)


def test_an_updated_dashboard_is_restored_not_deleted(app_context: None) -> None:
    from superset.views.ai_prompt import dashboard_publish as publish

    calls: list = []
    publisher = _publisher(publish, calls)
    original = _dashboard()

    with (
        mock.patch.object(
            publish, "UpdateDashboardCommand", _Recording(calls, "update_dashboard")
        ),
        mock.patch.object(publish, "DeleteDashboardCommand", _Recording(calls, "drop_dashboard")),
    ):
        publisher.update_dashboard(original, {"dashboard_title": "Edited"})
        outcomes = publisher.compensate()

    assert not [name for name, _ in calls if name == "drop_dashboard"]
    updates = [args for name, args in calls if name == "update_dashboard"]
    assert len(updates) == 2
    assert updates[-1][0] == 5
    assert updates[-1][1]["dashboard_title"] == "Original"
    assert updates[-1][1]["position_json"] == json.dumps(POSITION)
    assert updates[-1][1]["owners"] == [1]
    assert updates[-1][1]["roles"] == [2]
    assert outcomes[0]["outcome"] == "restored"


def test_a_failed_delete_is_reported_as_left_behind(app_context: None) -> None:
    from superset.views.ai_prompt import dashboard_publish as publish

    calls: list = []
    publisher = _publisher(publish, calls)
    publisher.ensure_chart(SimpleNamespace(id=3), {"title": "One"})

    with (
        mock.patch.object(
            publish,
            "CreateDashboardCommand",
            _Recording(calls, "create_dashboard", SimpleNamespace(id=90)),
        ),
        mock.patch.object(publish, "DeleteDashboardCommand", _Boom(calls, "drop_dashboard")),
        mock.patch.object(publish, "DeleteChartCommand", _Recording(calls, "drop_chart")),
    ):
        publisher.create_dashboard({"dashboard_title": "New"})
        outcomes = publisher.compensate()

    assert outcomes[0]["outcome"] == "left_behind:RuntimeError"
    # One failed object does not stop the rest of the unwind.
    assert outcomes[1]["outcome"] == "deleted"


def test_a_duplicate_records_the_copy_as_the_created_object(
    app_context: None,
) -> None:
    from superset.views.ai_prompt import dashboard_publish as publish

    calls: list = []
    publisher = _publisher(publish, calls)
    original = _dashboard(identifier=5)

    with mock.patch.object(
        publish,
        "CopyDashboardCommand",
        _Recording(calls, "copy_dashboard", _dashboard(identifier=91, title="New")),
    ):
        copy = publisher.duplicate_dashboard(
            original,
            {"dashboard_title": "New", "json_metadata": "{}", "owners": [1]},
        )

    assert copy.id == 91
    assert publisher.dashboard_id == 91
    entries = publisher.created_objects()
    assert entries[0]["kind"] == "dashboard"
    assert entries[0]["id"] == 91
    assert entries[0]["detail"] == "copied from 5"


def test_the_ledger_reports_entries_with_no_outcome(app_context: None) -> None:
    from superset.views.ai_prompt import dashboard_publish as publish

    calls: list = []
    publisher = _publisher(publish, calls)
    publisher.ensure_chart(SimpleNamespace(id=3), {"title": "One"})
    publisher.ledger.resolve("chart", 7, "deleted")

    assert publisher.ledger.unchanged() == []
    assert publisher.created_objects()[0]["outcome"] == "deleted"


def test_the_payload_carries_access_and_the_published_flag(app_context: None) -> None:
    from superset.views.ai_prompt import dashboard_publish as publish

    payload = publish.dashboard_payload(
        title="T" * 400,
        position_json=json.dumps(POSITION),
        metadata={"native_filters": [{"name": "region"}]},
        owners=[1, 2],
        roles=[3],
        slug="s" * 400,
        description="d" * 2_000,
    )

    assert len(payload["dashboard_title"]) == publish.MAX_TITLE_CHARS
    assert len(payload["slug"]) == publish.MAX_SLUG_CHARS
    assert len(payload["description"]) == publish.MAX_DESCRIPTION_CHARS
    assert payload["owners"] == [1, 2]
    assert payload["roles"] == [3]
    assert payload["published"] is True
    assert json.loads(payload["json_metadata"]) == {
        "native_filters": [{"name": "region"}]
    }


def test_only_a_layout_that_exists_has_a_live_position(app_context: None) -> None:
    from superset.views.ai_prompt import dashboard_publish as publish

    assert publish.live_position(None) is None
    assert publish.live_position(_dashboard(position="")) == {}


def test_the_policy_never_offers_an_access_control_the_build_lacks(
    app_context: None,
) -> None:
    from superset.views.ai_prompt import dashboard_publish as publish

    cases = [
        ({"DASHBOARD_RBAC": False, "EMBEDDED_SUPERSET": False}, False, False),
        ({"DASHBOARD_RBAC": True, "EMBEDDED_SUPERSET": False}, True, False),
        ({"DASHBOARD_RBAC": True, "EMBEDDED_SUPERSET": True}, True, True),
    ]
    for flags, expected_roles, expected_embed in cases:
        with mock.patch("superset.is_feature_enabled", side_effect=flags.get):
            policy = publish.policy_payload()

        assert policy["can_assign_roles"] is expected_roles, flags
        assert policy["can_share_publicly"] is expected_roles, flags
        assert policy["can_embed"] is expected_embed, flags
        assert policy["can_publish"] is True
