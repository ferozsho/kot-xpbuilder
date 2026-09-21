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
"""Publish Report Designer reports as Superset charts on dashboards.

Turns a saved report into a Superset chart backed by a virtual dataset built
from the report's SQL, then attaches it to an existing dashboard (or a freshly
created one). The published metadata (dataset/chart/dashboard ids) is persisted
on the report row so it can be re-published or un-published.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any

from flask_babel import gettext as _
from sqlalchemy import and_

from superset import db
from superset.connectors.sqla.models import SqlMetric, SqlaTable
from superset.daos.chart import ChartDAO
from superset.daos.dashboard import DashboardDAO
from superset.models.core import Database
from superset.models.dashboard import Dashboard
from superset.models.report_designer import ReportDesigner
from superset.models.slice import Slice

from .sql_builder import build_select, compile_sql, ReportDesignerError

logger = logging.getLogger(__name__)

SAFE_SLUG_RE = re.compile(r"[^a-zA-Z0-9_]+")

#: Grid layout constants (match the frontend dashboard util).
GRID_COLUMN_COUNT = 12
GRID_DEFAULT_CHART_WIDTH = 4
CHART_HEIGHT = 50


class PublishError(Exception):
    """Raised when a report cannot be published."""


# Viz catalog: friendly key -> Superset viz_type + param kind.
# kind "columns" renders raw columns (table); "aggregate" uses metrics+groupby;
# "metric" renders a single metric (big number). requires_dttm vizes need a
# date/time column in the report output.
#
# NOTE: the viz keys MUST match plugins registered by the fork's frontend
# (superset-frontend/src/visualizations/presets/MainPreset.ts). This fork has
# NO plain "echarts_bar" plugin — bars are "echarts_timeseries_bar" (a
# timeseries bar, hence requires_dttm too).
VIZ_CATALOG: dict[str, dict[str, Any]] = {
    "table": {"label": _("Table"), "kind": "columns", "viz": "table"},
    "pie": {"label": _("Pie"), "kind": "aggregate", "viz": "pie"},
    "bar": {
        "label": _("Bar"),
        "kind": "aggregate",
        "viz": "echarts_timeseries_bar",
        "requires_dttm": True,
    },
    "line": {
        "label": _("Line"),
        "kind": "aggregate",
        "viz": "echarts_timeseries_line",
        "requires_dttm": True,
    },
    "area": {
        "label": _("Area"),
        "kind": "aggregate",
        "viz": "echarts_area",
        "requires_dttm": True,
    },
    "treemap": {"label": _("Treemap"), "kind": "aggregate", "viz": "treemap_v2"},
    "sunburst": {"label": _("Sunburst"), "kind": "aggregate", "viz": "sunburst_v2"},
    "funnel": {"label": _("Funnel"), "kind": "aggregate", "viz": "funnel"},
    "radar": {"label": _("Radar"), "kind": "aggregate", "viz": "radar"},
    "box_plot": {"label": _("Box Plot"), "kind": "aggregate", "viz": "box_plot"},
    "histogram": {"label": _("Histogram"), "kind": "aggregate", "viz": "histogram_v2"},
    "scatter": {
        "label": _("Scatter"),
        "kind": "aggregate",
        "viz": "echarts_timeseries_scatter",
    },
    "big_number": {
        "label": _("Big Number"),
        "kind": "metric",
        "viz": "big_number",
    },
    "big_number_total": {
        "label": _("Big Number Total"),
        "kind": "metric",
        "viz": "big_number_total",
    },
}

_NUMERIC_PREFIXES = (
    "int",
    "float",
    "decimal",
    "double",
    "numeric",
    "bigint",
    "smallint",
    "tinyint",
    "mediumint",
)


def _slugify(value: str) -> str:
    """Turn an arbitrary string into a safe DB/slug identifier."""
    slug = SAFE_SLUG_RE.sub("-", str(value).strip().lower())
    return slug[:100] or "dashboard"


def _is_numeric_type(col_type: str | None) -> bool:
    if not col_type:
        return False
    normalized = str(col_type).lower()
    return any(normalized.startswith(prefix) for prefix in _NUMERIC_PREFIXES)


def _generate_id(prefix: str) -> str:
    """Generate a component id matching the frontend's nanoid-style pattern."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Dashboard layout helpers (mirror the fork's MCP dashboard layout logic)
# ---------------------------------------------------------------------------


def _layout_tab_target(layout: dict[str, Any]) -> str | None:
    """Return the first TAB id under a TABS container, if the layout is tabbed."""
    for container_id in ("ROOT_ID", "GRID_ID"):
        container = layout.get(container_id)
        if not isinstance(container, dict):
            continue
        for child_id in container.get("children", []):
            child = layout.get(child_id)
            if not isinstance(child, dict) or child.get("type") != "TABS":
                continue
            for tab_id in child.get("children", []):
                tab = layout.get(tab_id)
                if isinstance(tab, dict) and tab.get("type") == "TAB":
                    return tab_id
    return None


def _layout_add_chart(layout: dict[str, Any], chart: Slice) -> None:
    """Insert a chart node into a dashboard layout (ROOT→GRID→ROW→COLUMN→CHART).

    Handles both tabbed (ROOT→GRID→TABS→TAB) and flat (ROOT→GRID) layouts,
    creating missing containers as needed.
    """
    parent_id = _layout_tab_target(layout) or "GRID_ID"

    row_key = _generate_id("ROW")
    column_key = _generate_id("COLUMN")
    chart_key = f"CHART-{chart.id}"

    parent_component = layout.get(parent_id)
    if not isinstance(parent_component, dict):
        parent_component = None
    parent_parents = (
        list(parent_component.get("parents", [])) if parent_component else []
    )
    if parent_id == "GRID_ID" and not parent_parents:
        parent_parents = ["ROOT_ID"]
    row_parents = parent_parents + [parent_id]
    column_parents = row_parents + [row_key]
    chart_parents = column_parents + [column_key]

    layout[chart_key] = {
        "children": [],
        "id": chart_key,
        "meta": {
            "chartId": chart.id,
            "height": CHART_HEIGHT,
            "sliceName": chart.slice_name or f"Chart {chart.id}",
            "width": GRID_DEFAULT_CHART_WIDTH,
        },
        "parents": chart_parents,
        "type": "CHART",
    }
    layout[column_key] = {
        "children": [chart_key],
        "id": column_key,
        "meta": {"background": "BACKGROUND_TRANSPARENT", "width": GRID_COLUMN_COUNT},
        "parents": column_parents,
        "type": "COLUMN",
    }
    layout[row_key] = {
        "children": [column_key],
        "id": row_key,
        "meta": {"background": "BACKGROUND_TRANSPARENT"},
        "parents": row_parents,
        "type": "ROW",
    }

    # Attach the row to its parent container.
    if parent_component is not None:
        parent_component.setdefault("children", []).append(row_key)
    elif parent_id == "GRID_ID":
        # GRID_ID doesn't exist yet — create it below and add the row.
        layout.setdefault("GRID_ID", {"children": [], "id": "GRID_ID", "type": "GRID"})
        layout["GRID_ID"].setdefault("children", []).append(row_key)
    else:
        # Tab target referenced but not present — fall back to GRID.
        layout.setdefault("GRID_ID", {"children": [], "id": "GRID_ID", "type": "GRID"})
        layout["GRID_ID"].setdefault("children", []).append(row_key)

    # Ensure GRID + ROOT exist and are wired together.
    if "GRID_ID" not in layout:
        layout["GRID_ID"] = {
            "children": [],
            "id": "GRID_ID",
            "parents": ["ROOT_ID"],
            "type": "GRID",
        }
    if "ROOT_ID" not in layout:
        layout["ROOT_ID"] = {
            "children": ["GRID_ID"],
            "id": "ROOT_ID",
            "type": "ROOT",
        }
    else:
        root_children = layout["ROOT_ID"].setdefault("children", [])
        has_tabs_under_root = any(
            layout.get(child, {}).get("type") == "TABS" for child in root_children
        )
        if not has_tabs_under_root and "GRID_ID" not in root_children:
            root_children.append("GRID_ID")

    layout.setdefault("DASHBOARD_VERSION_KEY", "v2")


def _layout_remove_chart(layout: dict[str, Any], chart_id: int | None) -> None:
    """Remove a CHART node and any empty COLUMN/ROW parents from a layout."""
    chart_key = f"CHART-{chart_id}"
    if chart_key not in layout:
        return
    layout.pop(chart_key, None)

    for col_key, column in list(layout.items()):
        if not isinstance(column, dict):
            continue
        if column.get("type") != "COLUMN" or chart_key not in column.get("children", []):
            continue
        column["children"].remove(chart_key)
        if column["children"]:
            break
        layout.pop(col_key, None)
        # Drop the now-empty row (and its reference from the parent).
        for row_key, row in list(layout.items()):
            if not isinstance(row, dict):
                continue
            if row.get("type") != "ROW" or col_key not in row.get("children", []):
                continue
            row["children"].remove(col_key)
            if row["children"]:
                break
            layout.pop(row_key, None)
            for parent_key, parent in list(layout.items()):
                if not isinstance(parent, dict):
                    continue
                if parent.get("type") in ("GRID", "TAB") and row_key in parent.get(
                    "children", []
                ):
                    parent["children"].remove(row_key)
            break
        break


# ---------------------------------------------------------------------------
# Dataset + metrics
# ---------------------------------------------------------------------------


def _find_or_create_dataset(
    report: ReportDesigner,
    sql: str,
    database: Database,
) -> SqlaTable:
    """Create (or reuse) the virtual dataset backing a published report."""
    base = SAFE_SLUG_RE.sub("_", report.name).strip("_").lower() or "report"
    table_name = f"rd_{report.id}_{base}"[:120]

    dataset = (
        db.session.query(SqlaTable)
        .filter(
            and_(
                SqlaTable.database_id == database.id,
                SqlaTable.table_name == table_name,
            )
        )
        .first()
    )
    if dataset is None:
        dataset = SqlaTable(
            database_id=database.id,
            schema=None,
            table_name=table_name,
            sql=sql,
        )
        db.session.add(dataset)
        db.session.commit()
    else:
        dataset.sql = sql
        db.session.commit()

    try:
        dataset.fetch_metadata()
        db.session.commit()
    except Exception:  # pylint: disable=broad-except
        logger.warning("Metadata fetch failed for report dataset %s", table_name)
        db.session.rollback()
    return dataset


def _ensure_metrics(dataset: SqlaTable, definition: dict[str, Any]) -> None:
    """Register report metrics on the dataset so chart params can use their names.

    The published virtual dataset already returns the aggregated output columns
    (the inner report SQL computes them), so chart metrics MUST reference those
    OUTPUT columns — not the source-table columns, which do not exist in the
    virtual dataset (referencing ``userid`` etc. there raises "Unknown column").
    Aggregating an already-aggregated column is a no-op per group, so we
    register ``SUM(<output column>)`` (``AVG`` for AVG metrics) as the
    expression.
    """
    existing = {metric.metric_name for metric in dataset.metrics}
    wanted: dict[str, str] = {"count": "COUNT(*)"}
    for metric in definition.get("metrics") or []:
        label = str(metric.get("label") or "").strip()
        aggregate = str(metric.get("aggregate") or "SUM").upper()
        if label:
            quoted = f"`{label}`"
            wanted[label] = f"AVG({quoted})" if aggregate == "AVG" else f"SUM({quoted})"

    changed = False
    for name, expression in wanted.items():
        metric = next(
            (m for m in dataset.metrics if m.metric_name == name), None
        )
        if metric is None:
            dataset.metrics.append(
                SqlMetric(
                    metric_name=name,
                    verbose_name=name,
                    expression=expression,
                )
            )
            changed = True
        elif metric.expression != expression:
            # Re-publish may previously have stored a source-column expression;
            # rewrite it to reference the output column instead.
            metric.expression = expression
            changed = True
    if changed:
        db.session.commit()


def _build_params(
    viz_key: str,
    dataset: SqlaTable,
    definition: dict[str, Any],
) -> dict[str, Any]:
    """Build chart form_data params for a viz type from the report definition."""
    catalog = VIZ_CATALOG[viz_key]
    kind = catalog["kind"]
    row_limit = int(definition.get("limit") or 1000)

    column_names = [column.column_name for column in dataset.columns]
    report_labels = [
        column.get("label") or column.get("column")
        for column in definition.get("columns") or []
    ]
    report_metric_labels = [
        metric.get("label") for metric in definition.get("metrics") or []
    ]
    numeric_cols = [
        column.column_name
        for column in dataset.columns
        if _is_numeric_type(column.type)
    ]

    dttm_col = dataset.main_dttm_col
    if not dttm_col:
        for column in dataset.columns:
            if column.is_dttm or column.is_temporal:
                dttm_col = column.column_name
                break

    if catalog.get("requires_dttm") and not dttm_col:
        raise PublishError(
            _(
                "Viz type %(label)s needs a date/time column in the report "
                "output. Add a date field (e.g. from a timestamp column) or "
                "pick a non-time chart type.",
                label=catalog["label"],
            )
        )

    params: dict[str, Any] = {
        "viz_type": catalog["viz"],
        "datasource": f"{dataset.id}__table",
        "row_limit": row_limit,
    }

    if kind == "columns":
        all_columns = [label for label in report_labels if label in column_names]
        params["all_columns"] = all_columns or column_names
        params["order_by_cols"] = []
    elif kind == "metric":
        metric = next((label for label in report_metric_labels if label), None) or "count"
        params["metric"] = metric
        if dttm_col:
            params["granularity_sqla"] = dttm_col
            params["time_grain_sqla"] = "P1D"
    else:  # aggregate
        groupby = [label for label in report_labels if label in column_names]
        if not groupby:
            groupby = [name for name in column_names if name not in report_metric_labels][
                :5
            ]
        metrics = [label for label in report_metric_labels if label] or ["count"]

        if viz_key == "pie":
            params["metric"] = metrics[0]
            params["groupby"] = groupby
        elif viz_key == "histogram":
            params["column"] = (
                numeric_cols[0]
                if numeric_cols
                else (column_names[0] if column_names else None)
            )
            params["groupby"] = groupby
        elif viz_key == "scatter":
            params["x"] = numeric_cols[0] if numeric_cols else None
            params["y"] = (
                numeric_cols[1]
                if len(numeric_cols) > 1
                else (numeric_cols[0] if numeric_cols else None)
            )
            if groupby:
                params["series"] = groupby[0]
        else:
            params["metrics"] = metrics
            params["groupby"] = groupby
            if viz_key in ("bar", "line", "area") and dttm_col:
                params["granularity_sqla"] = dttm_col
                params["time_grain_sqla"] = "P1D"

    return params


# ---------------------------------------------------------------------------
# Publish / unpublish
# ---------------------------------------------------------------------------


def _attach_chart_to_dashboard(dashboard: Dashboard, chart: Slice) -> None:
    """Attach a chart to a dashboard and add it to the rendered layout."""
    if chart not in dashboard.slices:
        dashboard.slices.append(chart)

    layout: dict[str, Any] = {}
    if dashboard.position_json:
        try:
            layout = json.loads(dashboard.position_json)
        except (TypeError, ValueError):
            layout = {}
    if not isinstance(layout, dict):
        layout = {}
    # Idempotent: skip when the chart node is already in the layout.
    if f"CHART-{chart.id}" not in layout:
        _layout_add_chart(layout, chart)
    dashboard.position_json = json.dumps(layout)


def _detach_chart_from_dashboard(chart: Slice, dashboard: Dashboard) -> None:
    """Detach a chart from one dashboard (slices + rendered layout)."""
    if chart in dashboard.slices:
        dashboard.slices.remove(chart)

    layout: dict[str, Any] = {}
    if dashboard.position_json:
        try:
            layout = json.loads(dashboard.position_json)
        except (TypeError, ValueError):
            layout = {}
    if isinstance(layout, dict):
        _layout_remove_chart(layout, chart.id)
        dashboard.position_json = json.dumps(layout)


def _detach_chart(chart: Slice, exclude_dashboard: Dashboard | None = None) -> None:
    """Detach a chart from every dashboard it is on, except ``exclude_dashboard``."""
    for dashboard in list(chart.dashboards):
        if exclude_dashboard is not None and dashboard.id == exclude_dashboard.id:
            continue
        _detach_chart_from_dashboard(chart, dashboard)
    db.session.commit()
    db.session.commit()


def publish_report(
    report: ReportDesigner,
    viz_key: str,
    chart_name: str | None = None,
    dashboard_id: int | None = None,
    new_dashboard_name: str | None = None,
    user: Any = None,
) -> dict[str, Any]:
    """Publish a saved report as a Superset chart on a dashboard."""
    viz_key = viz_key or "table"
    if viz_key not in VIZ_CATALOG:
        raise PublishError(_("Unsupported visualization type: %(viz)s", viz=viz_key))

    definition = report.get_definition()
    try:
        stmt, primary = build_select(definition)
        database = primary.database
        sql = compile_sql(stmt, database)
    except ReportDesignerError as ex:
        raise PublishError(str(ex)) from ex

    # Virtual dataset from the report's SQL.
    dataset = _find_or_create_dataset(report, sql, database)
    _ensure_metrics(dataset, definition)

    # Chart — reuse the existing one on republish, otherwise create fresh.
    params = _build_params(viz_key, dataset, definition)
    slice_name = (chart_name or report.name).strip() or "Untitled chart"
    chart = None
    if report.chart_id:
        chart = db.session.get(Slice, report.chart_id)
    created = chart is None
    if created:
        chart = Slice(
            slice_name=slice_name,
            datasource_id=dataset.id,
            datasource_type="table",
            datasource_name=dataset.table_name,
            viz_type=VIZ_CATALOG[viz_key]["viz"],
            params=json.dumps(params),
            owners=[user] if user is not None else [],
        )
        db.session.add(chart)
        db.session.flush()
    else:
        chart.slice_name = slice_name
        chart.datasource_id = dataset.id
        chart.datasource_type = "table"
        chart.datasource_name = dataset.table_name
        chart.viz_type = VIZ_CATALOG[viz_key]["viz"]
        chart.params = json.dumps(params)
        if user is not None and user not in chart.owners:
            chart.owners.append(user)
    db.session.commit()

    # Dashboard (existing or new).
    dashboard: Dashboard | None = None
    if dashboard_id:
        dashboard = DashboardDAO.find_by_id(dashboard_id, skip_base_filter=True)
        if dashboard is None:
            if created:
                db.session.delete(chart)
                db.session.commit()
            raise PublishError(_("Dashboard %(id)s not found", id=dashboard_id))
        _detach_chart(chart, exclude_dashboard=dashboard)
        _attach_chart_to_dashboard(dashboard, chart)
    elif new_dashboard_name:
        dashboard = Dashboard(
            dashboard_title=(new_dashboard_name or report.name).strip(),
            slug=_slugify(new_dashboard_name or report.name),
            owners=[user] if user is not None else [],
            published=True,
        )
        DashboardDAO.create(dashboard)
        db.session.commit()
        _detach_chart(chart)
        _attach_chart_to_dashboard(dashboard, chart)
    else:
        # No dashboard requested — detach from any previously attached one.
        _detach_chart(chart)

    # Persist publish metadata on the report.
    report.dataset_id = dataset.id
    report.chart_id = chart.id
    report.dashboard_id = dashboard.id if dashboard else None
    report.viz_type = VIZ_CATALOG[viz_key]["viz"]
    report.chart_name = slice_name
    report.published_at = datetime.now()
    db.session.commit()

    return {
        "report_id": report.id,
        "dataset_id": dataset.id,
        "chart_id": chart.id,
        "viz_type": VIZ_CATALOG[viz_key]["viz"],
        "chart_name": slice_name,
        "dashboard_id": dashboard.id if dashboard else None,
        "dashboard_title": dashboard.dashboard_title if dashboard else None,
        "explore_url": f"/explore/?slice_id={chart.id}",
        "dashboard_url": (
            f"/superset/dashboard/{dashboard.id}/" if dashboard else None
        ),
    }


def unpublish_report(report: ReportDesigner) -> dict[str, Any]:
    """Detach the published chart/dashboard/dataset and clear report metadata."""
    chart_id = report.chart_id
    dashboard_id = report.dashboard_id
    dataset_id = report.dataset_id

    if dashboard_id:
        dashboard = DashboardDAO.find_by_id(dashboard_id, skip_base_filter=True)
        if dashboard is not None:
            dashboard.slices = [
                chart for chart in dashboard.slices if chart.id != chart_id
            ]
            layout: dict[str, Any] = {}
            if dashboard.position_json:
                try:
                    layout = json.loads(dashboard.position_json)
                except (TypeError, ValueError):
                    layout = {}
            _layout_remove_chart(layout, chart_id)
            dashboard.position_json = json.dumps(layout) if isinstance(layout, dict) else "{}"
            db.session.commit()

    if chart_id:
        chart = db.session.get(Slice, chart_id)
        if chart is not None:
            ChartDAO.delete([chart])
            db.session.commit()

    if dataset_id:
        dataset = db.session.get(SqlaTable, dataset_id)
        if dataset is not None:
            db.session.delete(dataset)
            db.session.commit()

    report.dataset_id = None
    report.chart_id = None
    report.dashboard_id = None
    report.viz_type = None
    report.chart_name = None
    report.published_at = None
    db.session.commit()
    return {"message": _("Report unpublished")}
