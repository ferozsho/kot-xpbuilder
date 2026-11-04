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
"""Publish a generated dashboard inside one tracked, compensable attempt.

Every other Phase 8 step is a check. This one creates objects, so it is written
the way a database transaction is written: each object is recorded with its id
the moment it exists, and a failure undoes what was done in reverse order. A
publish that stops halfway therefore leaves either nothing or a row that says
exactly what it left behind (P8-F10).

Why the dashboard is deleted before the charts: Superset refuses to delete a
chart a dashboard still references, so compensation has to unwind in the same
order it built — dashboard first, then charts.

Why an update is snapshotted rather than deleted: the dashboard existed before
this attempt and must still exist after a failed one. The previous title,
position, metadata and access are captured first and written back if the update
fails, so "compensate" never means "throw away the user's dashboard" (P8-F11).

The mode is never inferred (P8-F08): ``create`` builds a new dashboard,
``update`` edits one in place, ``duplicate`` copies one under a new title. Access
is assigned from the decision the service already made (P8-F09) — this module
applies it, it does not decide it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional

from superset.commands.chart.create import CreateChartCommand
from superset.commands.chart.delete import DeleteChartCommand
from superset.commands.dashboard.copy import CopyDashboardCommand
from superset.commands.dashboard.create import CreateDashboardCommand
from superset.commands.dashboard.delete import DeleteDashboardCommand
from superset.commands.dashboard.update import UpdateDashboardCommand
from superset.utils.json import dumps as json_dumps

logger = logging.getLogger(__name__)

MODE_CREATE = "create"
MODE_UPDATE = "update"
MODE_DUPLICATE = "duplicate"

MAX_TITLE_CHARS = 250
MAX_DESCRIPTION_CHARS = 1_000
MAX_SLUG_CHARS = 128

#: How an object this attempt created ended up. ``deleted`` and ``restored`` are
#: clean; ``left_behind`` is the one outcome an operator must be told about.
CLEAN_OUTCOMES = frozenset({"deleted", "restored", "kept"})


class PublishBlockedError(RuntimeError):
    """A pre-condition failed before anything was created; ``code`` is stable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class Ledger:
    """Every object this attempt touched, in the order it was touched."""

    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        kind: str,
        identifier: Any,
        *,
        action: str = "create",
        detail: str = "",
    ) -> dict[str, Any]:
        entry = {
            "kind": kind,
            "id": identifier,
            "action": action,
            "outcome": "",
            "detail": detail[:160],
        }
        self.entries.append(entry)
        return entry

    def resolve(
        self, kind: str, identifier: Any, outcome: str, detail: str = ""
    ) -> None:
        """Attach the outcome to the entry for one object, newest first."""
        for entry in reversed(self.entries):
            if entry["kind"] == kind and entry["id"] == identifier:
                entry["outcome"] = outcome[:64]
                entry["detail"] = (detail or entry["detail"])[:160]
                return
        self.entries.append(
            {
                "kind": kind,
                "id": identifier,
                "action": "create",
                "outcome": outcome[:64],
                "detail": detail[:160],
            }
        )

    def created(self, kind: str) -> list[int]:
        return [
            int(entry["id"])
            for entry in self.entries
            if entry["kind"] == kind
            and entry["action"] == "create"
            and entry["id"] is not None
        ]

    def unchanged(self) -> list[dict[str, Any]]:
        """Entries that still carry no outcome, for a failure report."""
        return [dict(entry) for entry in self.entries if not entry["outcome"]]


def policy_payload() -> dict[str, Any]:
    """The capabilities this deployment can actually honour (P8-F09).

    Roles and guest embedding are reported only when the build supports them,
    because offering an access control the deployment ignores is worse than not
    offering it: the publisher would believe the dashboard was restricted.
    """
    from superset import is_feature_enabled

    rbac = bool(is_feature_enabled("DASHBOARD_RBAC"))
    return {
        "can_publish": True,
        "can_assign_roles": rbac,
        "can_share_publicly": rbac,
        "can_embed": bool(is_feature_enabled("EMBEDDED_SUPERSET")),
    }


def dashboard_payload(
    *,
    title: str,
    position_json: str,
    metadata: Mapping[str, Any],
    owners: Sequence[int],
    roles: Sequence[int],
    slug: str = "",
    description: str = "",
) -> dict[str, Any]:
    """The command payload shared by create, update and duplicate."""
    body: dict[str, Any] = {
        "dashboard_title": title[:MAX_TITLE_CHARS],
        "position_json": position_json,
        "json_metadata": json_dumps(dict(metadata)),
        "owners": [int(owner) for owner in owners],
        "roles": [int(role) for role in roles],
        "published": True,
    }
    if slug:
        body["slug"] = slug[:MAX_SLUG_CHARS]
    if description:
        body["description"] = description[:MAX_DESCRIPTION_CHARS]
    return body


class DashboardPublisher:
    """Create, update or duplicate a dashboard, keeping a ledger of every write."""

    def __init__(
        self,
        owner: int,
        *,
        chart_factory: Optional[Callable[[int, Any, dict[str, Any]], Any]] = None,
    ) -> None:
        self.owner = int(owner)
        self.ledger = Ledger()
        self._chart_factory = chart_factory or _create_chart
        self._snapshot: dict[str, Any] | None = None
        self._dashboard_id: int | None = None
        self._created_dashboard: int | None = None
        self._dashboard: Any = None

    # --- objects -------------------------------------------------------

    def ensure_chart(self, dataset: Any, draft: Mapping[str, Any]) -> Any:
        """Create the chart a generated slot needs, recording its id first."""
        chart = self._chart_factory(self.owner, dataset, dict(draft))
        self.ledger.record(
            "chart",
            int(chart.id),
            detail=str(draft.get("title") or "")[:160],
        )
        logger.info("dashboard_publish chart_created id=%s", int(chart.id))
        return chart

    def create_dashboard(self, payload: Mapping[str, Any]) -> Any:
        dashboard = CreateDashboardCommand(dict(payload)).run()
        self._dashboard = dashboard
        self._dashboard_id = int(dashboard.id)
        self._created_dashboard = int(dashboard.id)
        self.ledger.record("dashboard", int(dashboard.id), detail="created")
        logger.info("dashboard_publish dashboard_created id=%s", int(dashboard.id))
        return dashboard

    def update_dashboard(self, dashboard: Any, payload: Mapping[str, Any]) -> Any:
        """Edit in place, after capturing what a failure must put back."""
        self._snapshot = _snapshot(dashboard)
        self._dashboard_id = int(dashboard.id)
        self.ledger.record(
            "dashboard",
            int(dashboard.id),
            action="update",
            detail="updated",
        )
        updated = UpdateDashboardCommand(int(dashboard.id), dict(payload)).run()
        self._dashboard = updated
        logger.info("dashboard_publish dashboard_updated id=%s", int(dashboard.id))
        return updated

    def duplicate_dashboard(self, dashboard: Any, payload: Mapping[str, Any]) -> Any:
        """Copy in place; the copy is this attempt's object, the original is not."""
        self._snapshot = _snapshot(dashboard)
        body = dict(payload)
        body.setdefault("json_metadata", json_dumps({}))
        copy = CopyDashboardCommand(dashboard, body).run()
        self._dashboard = copy
        self._dashboard_id = int(copy.id)
        self._created_dashboard = int(copy.id)
        self.ledger.record(
            "dashboard",
            int(copy.id),
            detail=f"copied from {int(dashboard.id)}",
        )
        logger.info(
            "dashboard_publish dashboard_duplicated id=%s from=%s",
            int(copy.id),
            int(dashboard.id),
        )
        return copy

    # --- compensation --------------------------------------------------

    def compensate(self) -> list[dict[str, Any]]:
        """Undo this attempt in reverse order, recording what happened to each id.

        Nothing here raises: a compensation that fails must still report, because
        an object nobody can account for is the failure this method exists to
        prevent.
        """
        outcomes: list[dict[str, Any]] = []

        if self._created_dashboard is not None:
            outcomes.append(self._drop_dashboard(self._created_dashboard))
        elif self._snapshot is not None and self._dashboard_id is not None:
            outcomes.append(self._restore_dashboard(self._snapshot, self._dashboard_id))

        for chart_id in reversed(self.ledger.created("chart")):
            outcomes.append(self._drop_chart(chart_id))

        leftovers = self.ledger.unchanged()
        if leftovers:
            logger.warning(
                "dashboard_publish left_behind %s", [e["kind"] for e in leftovers]
            )
        return outcomes

    def _drop_dashboard(self, dashboard_id: int) -> dict[str, Any]:
        try:
            DeleteDashboardCommand(dashboard_id).run()
        except Exception as exc:  # pylint: disable=broad-except
            return self._outcome(
                "dashboard", dashboard_id, f"left_behind:{type(exc).__name__}"
            )
        return self._outcome("dashboard", dashboard_id, "deleted")

    def _restore_dashboard(
        self, snapshot: Mapping[str, Any], dashboard_id: int
    ) -> dict[str, Any]:
        try:
            UpdateDashboardCommand(dashboard_id, dict(snapshot["data"])).run()
        except Exception as exc:  # pylint: disable=broad-except
            return self._outcome(
                "dashboard", dashboard_id, f"left_behind:restore_{type(exc).__name__}"
            )
        return self._outcome(
            "dashboard", dashboard_id, "restored", snapshot["detail"]
        )

    def _drop_chart(self, chart_id: int) -> dict[str, Any]:
        try:
            DeleteChartCommand(chart_id).run()
        except Exception as exc:  # pylint: disable=broad-except
            return self._outcome(
                "chart", chart_id, f"left_behind:{type(exc).__name__}"
            )
        return self._outcome("chart", chart_id, "deleted")

    def _outcome(
        self, kind: str, identifier: int, outcome: str, detail: str = ""
    ) -> dict[str, Any]:
        self.ledger.resolve(kind, identifier, outcome, detail)
        logger.info(
            "dashboard_publish cleanup kind=%s id=%s outcome=%s",
            kind,
            identifier,
            outcome,
        )
        return {"kind": kind, "id": identifier, "outcome": outcome, "detail": detail}

    # --- reporting -----------------------------------------------------

    @property
    def dashboard_id(self) -> int | None:
        return self._dashboard_id

    @property
    def dashboard(self) -> Any:
        """The dashboard this attempt wrote, once the command has returned."""
        return self._dashboard

    def created_objects(self) -> list[dict[str, Any]]:
        return [dict(entry) for entry in self.ledger.entries]


def _snapshot(dashboard: Any) -> dict[str, Any]:
    """The fields an update must be able to put back."""
    return {
        "data": {
            "dashboard_title": dashboard.dashboard_title,
            "position_json": dashboard.position_json,
            "json_metadata": dashboard.json_metadata,
            "owners": [int(owner.id) for owner in dashboard.owners],
            "roles": [int(role.id) for role in dashboard.roles],
        },
        "detail": "restored to the layout held before this attempt",
    }


def _create_chart(owner: int, dataset: Any, draft: Mapping[str, Any]) -> Any:
    """Create a chart through Superset's command, so its own validation applies."""
    data = {
        "slice_name": str(draft.get("title") or "")[:MAX_TITLE_CHARS],
        "viz_type": str(draft.get("viz_type") or ""),
        "datasource_type": "table",
        "datasource_id": int(dataset.id),
        "params": json_dumps(dict(draft.get("form_data") or {})),
        "description": str(draft.get("description") or "")[:MAX_DESCRIPTION_CHARS],
        "owners": [int(owner)],
        "dashboards": [],
    }
    return CreateChartCommand(data).run()


def live_position(dashboard: Any) -> Any:
    """The dashboard's layout as Superset holds it, for the drift check (F11)."""
    if dashboard is None:
        return None
    return dashboard.position_json or {}


__all__ = [
    "MODE_CREATE",
    "MODE_DUPLICATE",
    "MODE_UPDATE",
    "CLEAN_OUTCOMES",
    "DashboardPublisher",
    "Ledger",
    "PublishBlockedError",
    "dashboard_payload",
    "live_position",
    "policy_payload",
]
