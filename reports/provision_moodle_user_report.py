#!/usr/bin/env python3
"""Provision a Moodle user report on a database already connected to Superset.

Creates one curated virtual dataset plus a "User Report" dashboard through the
Superset REST API. The script is idempotent: it deletes and recreates only the
artifacts it owns (matched by the report marker and dashboard slug), so it can
be re-run against a live stack after the Moodle schema changes.

The dataset is defined as SQL, so it needs no changes to the connected
database and no write access to it.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests


MARKER = "XPBuilder Moodle user report"
DATASET_NAME = "Moodle User Report"
DASHBOARD_TITLE = "User Report"
DASHBOARD_SLUG = "moodle-user-report"

# Grain: exactly one row per (non-deleted, non-guest) Moodle user, so summing a
# 0/1 flag column gives a user count and averaging is safe. Moodle stores times
# as Unix timestamps, hence FROM_UNIXTIME; rows with a zero timestamp keep a
# NULL date instead of showing up as 1970.
DATASET_SQL = """
SELECT
    u.id AS user_id,
    u.username AS username,
    CONCAT(u.firstname, ' ', u.lastname) AS full_name,
    u.email AS email,
    u.phone1 AS mobile,
    u.open_employeeid AS employee_id,
    u.open_designation AS designation,
    u.gender AS gender_code,
    u.auth AS auth_method,
    CASE WHEN u.suspended = 1 THEN 'Suspended' ELSE 'Active' END
        AS account_status,
    CASE WHEN u.confirmed = 1 THEN 'Confirmed' ELSE 'Unconfirmed' END
        AS confirmation_status,
    CASE
        WHEN u.lastaccess = 0 THEN 'Never logged in'
        WHEN FROM_UNIXTIME(u.lastaccess) >= NOW() - INTERVAL 30 DAY
            THEN 'Active (0-30 days)'
        WHEN FROM_UNIXTIME(u.lastaccess) >= NOW() - INTERVAL 90 DAY
            THEN 'Active (31-90 days)'
        WHEN FROM_UNIXTIME(u.lastaccess) >= NOW() - INTERVAL 365 DAY
            THEN 'Active (91-365 days)'
        ELSE 'Dormant (over 1 year)'
    END AS activity_status,
    CASE r.role_rank
        WHEN 1 THEN 'Administrator'
        WHEN 2 THEN 'Manager'
        WHEN 3 THEN 'Project Manager'
        WHEN 4 THEN 'Operations Manager'
        WHEN 5 THEN 'Vertical Manager'
        WHEN 6 THEN 'Intervention Manager'
        WHEN 7 THEN 'Data Manager'
        WHEN 8 THEN 'Content Manager'
        WHEN 9 THEN 'Project Coordinator'
        WHEN 10 THEN 'Project SPOC'
        WHEN 11 THEN 'Editing Teacher'
        WHEN 12 THEN 'Trainer'
        WHEN 13 THEN 'Teacher'
        WHEN 14 THEN 'Course Creator'
        WHEN 15 THEN 'Learner'
        ELSE 'No role'
    END AS primary_role,
    COALESCE(r.role_short_names, 'No role') AS all_roles,
    COALESCE(e.enrolled_courses, 0) AS enrolled_courses,
    CASE WHEN u.timecreated > 0
        THEN DATE(FROM_UNIXTIME(u.timecreated)) END AS created_date,
    CASE WHEN u.timecreated > 0
        THEN DATE(DATE_FORMAT(FROM_UNIXTIME(u.timecreated), '%Y-%m-01')) END
        AS created_month,
    CASE WHEN u.lastaccess > 0
        THEN DATE(FROM_UNIXTIME(u.lastaccess)) END AS last_access_date,
    CASE WHEN u.lastlogin > 0
        THEN DATE(FROM_UNIXTIME(u.lastlogin)) END AS last_login_date,
    CASE WHEN u.lastaccess > 0
        THEN DATEDIFF(NOW(), FROM_UNIXTIME(u.lastaccess)) END
        AS days_since_last_access,
    CASE WHEN u.lastaccess = 0 THEN 1 ELSE 0 END AS never_logged_in_flag,
    CASE WHEN u.lastaccess > 0
        AND FROM_UNIXTIME(u.lastaccess) >= NOW() - INTERVAL 30 DAY
        THEN 1 ELSE 0 END AS active_30d_flag,
    CASE WHEN u.lastaccess > 0
        AND FROM_UNIXTIME(u.lastaccess) >= NOW() - INTERVAL 90 DAY
        THEN 1 ELSE 0 END AS active_90d_flag,
    CASE WHEN u.timecreated > 0
        AND FROM_UNIXTIME(u.timecreated) >= NOW() - INTERVAL 30 DAY
        THEN 1 ELSE 0 END AS new_30d_flag,
    CASE WHEN u.suspended = 1 THEN 1 ELSE 0 END AS suspended_flag,
    CASE WHEN r.role_rank = 15 THEN 1 ELSE 0 END AS learner_flag,
    CASE WHEN r.role_rank BETWEEN 1 AND 10 THEN 1 ELSE 0 END AS manager_flag,
    CASE WHEN r.role_rank BETWEEN 11 AND 14 THEN 1 ELSE 0 END AS educator_flag,
    CASE WHEN r.role_rank IS NULL THEN 1 ELSE 0 END AS no_role_flag,
    CASE WHEN COALESCE(e.enrolled_courses, 0) > 0 THEN 1 ELSE 0 END
        AS enrolled_flag
FROM mdl_user u
LEFT JOIN (
    SELECT ra.userid,
           GROUP_CONCAT(DISTINCT ro.shortname ORDER BY ro.shortname
                        SEPARATOR ', ') AS role_short_names,
           MIN(CASE ro.shortname
                   WHEN 'administrator' THEN 1
                   WHEN 'manager' THEN 2
                   WHEN 'projectmanager' THEN 3
                   WHEN 'operationalmanager' THEN 4
                   WHEN 'verticalmanager' THEN 5
                   WHEN 'interventionmanager' THEN 6
                   WHEN 'datamanager' THEN 7
                   WHEN 'contentmanager' THEN 8
                   WHEN 'projectcoordinator' THEN 9
                   WHEN 'projectspoc' THEN 10
                   WHEN 'editingteacher' THEN 11
                   WHEN 'trainer' THEN 12
                   WHEN 'teacher' THEN 13
                   WHEN 'coursecreator' THEN 14
                   WHEN 'employee' THEN 15
                   ELSE 99
               END) AS role_rank
      FROM mdl_role_assignments ra
      JOIN mdl_role ro ON ro.id = ra.roleid
     GROUP BY ra.userid
) r ON r.userid = u.id
LEFT JOIN (
    SELECT ue.userid, COUNT(DISTINCT en.courseid) AS enrolled_courses
      FROM mdl_user_enrolments ue
      JOIN mdl_enrol en ON en.id = ue.enrolid
     GROUP BY ue.userid
) e ON e.userid = u.id
WHERE u.deleted = 0 AND u.id > 1
"""

DETAIL_COLUMNS = (
    "username",
    "full_name",
    "email",
    "mobile",
    "employee_id",
    "designation",
    "primary_role",
    "all_roles",
    "activity_status",
    "account_status",
    "created_date",
    "last_access_date",
    "days_since_last_access",
    "enrolled_courses",
    "auth_method",
)


@dataclass
class ChartSpec:
    """A chart to create, with its width in the 12-column dashboard grid."""

    name: str
    form: dict[str, Any]
    width: int
    viz_type: str = field(init=False)

    def __post_init__(self) -> None:
        self.viz_type = self.form["viz_type"]


class SupersetApi:
    """Small checked client for the Superset REST API."""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        response = self.session.post(
            f"{self.base_url}/api/v1/security/login",
            json={
                "username": username,
                "password": password,
                "provider": "db",
                "refresh": True,
            },
            timeout=30,
        )
        self._check(response, "login")
        self.session.headers["Authorization"] = (
            f"Bearer {response.json()['access_token']}"
        )

    @staticmethod
    def _check(response: requests.Response, action: str) -> requests.Response:
        if response.ok:
            return response
        try:
            detail = response.json()
        except requests.JSONDecodeError:
            detail = response.text[:500]
        raise RuntimeError(f"{action} failed with HTTP {response.status_code}: {detail}")

    def get(self, path: str) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}{path}", timeout=60)
        return self._check(response, f"GET {path}").json()

    def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.post(f"{self.base_url}{path}", timeout=180, **kwargs)
        return self._check(response, f"POST {path}").json()

    def put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.put(f"{self.base_url}{path}", json=payload, timeout=180)
        return self._check(response, f"PUT {path}").json()

    def delete(self, path: str) -> None:
        response = self.session.delete(f"{self.base_url}{path}", timeout=60)
        self._check(response, f"DELETE {path}")

    def list_all(self, resource: str) -> list[dict[str, Any]]:
        result = self.get(f"/api/v1/{resource}/?q=(page:0,page_size:1000)")
        return result.get("result", [])


def load_env(path: Path) -> dict[str, str]:
    """Read a deployment ``.env`` file without sourcing it."""
    if path.name != ".env":
        raise ValueError("The environment file basename must be exactly .env")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key] = value
    return values


def metric(column: str, aggregate: str, label: str) -> dict[str, Any]:
    """Return a portable simple metric definition."""
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": column},
        "aggregate": aggregate,
        "label": label,
    }


def base_form(dataset_id: int, viz_type: str) -> dict[str, Any]:
    """Return the chart settings shared by every visualization."""
    return {
        "adhoc_filters": [],
        "color_scheme": "supersetColors",
        "datasource": f"{dataset_id}__table",
        "row_limit": 10000,
        "show_legend": True,
        "time_range": "No filter",
        "viz_type": viz_type,
    }


def big_number(
    dataset_id: int,
    metric_definition: dict[str, Any],
    label: str,
    *,
    number_format: str = "SMART_NUMBER",
) -> dict[str, Any]:
    form = base_form(dataset_id, "big_number_total")
    form.update(
        {
            "header_font_size": 0.42,
            "metric": metric_definition,
            "subheader": label,
            "subheader_font_size": 0.14,
            "y_axis_format": number_format,
        }
    )
    return form


def pie_chart(
    dataset_id: int, groupby: str, metric_definition: dict[str, Any]
) -> dict[str, Any]:
    form = base_form(dataset_id, "pie")
    form.update(
        {
            "donut": True,
            "groupby": [groupby],
            "innerRadius": 38,
            "label_type": "key_percent",
            "labels_outside": True,
            "metric": metric_definition,
            "number_format": "SMART_NUMBER",
            "show_labels": True,
            "show_total": False,
        }
    )
    return form


def bar_chart(
    dataset_id: int,
    x_axis: str,
    metric_definition: dict[str, Any],
    *,
    kind: str = "bar",
) -> dict[str, Any]:
    viz_type = "echarts_timeseries_line" if kind == "line" else "echarts_timeseries_bar"
    form = base_form(dataset_id, viz_type)
    form.update(
        {
            "metrics": [metric_definition],
            "rich_tooltip": True,
            "show_value": False,
            "x_axis": x_axis,
            "x_axis_label": x_axis.replace("_", " ").title(),
            "x_axis_label_rotation": 25 if kind == "bar" else 0,
            "x_axis_sort_series": "sum",
            "x_axis_sort_series_ascending": False,
            "y_axis_format": "SMART_NUMBER",
        }
    )
    return form


def month_line(
    dataset_id: int, x_axis: str, metric_definition: dict[str, Any]
) -> dict[str, Any]:
    """Return a monthly time series, ignoring rows without a signup date."""
    form = bar_chart(dataset_id, x_axis, metric_definition, kind="line")
    form.update(
        {
            "granularity_sqla": x_axis,
            "groupby": [],
            "time_grain_sqla": "P1M",
            "x_axis_time_format": "smart_date",
        }
    )
    return form


def table_chart(dataset_id: int, columns: list[str]) -> dict[str, Any]:
    form = base_form(dataset_id, "table")
    form.update(
        {
            "all_columns": columns,
            "include_search": True,
            "order_by_cols": [],
            "page_length": 25,
            "query_mode": "raw",
            "server_page_length": 25,
            "table_timestamp_format": "%Y-%m-%d",
        }
    )
    return form


def chart_specs(dataset_id: int) -> list[ChartSpec]:
    """Define every KPI, visualization, and detail table in the report."""
    users = metric("user_id", "COUNT_DISTINCT", "Users")
    kpis = (
        ("Total users", users),
        ("Learners", metric("learner_flag", "SUM", "Learners")),
        ("Active in last 30 days", metric("active_30d_flag", "SUM", "Active (30d)")),
        ("Never logged in", metric("never_logged_in_flag", "SUM", "Never logged in")),
        ("New in last 30 days", metric("new_30d_flag", "SUM", "New (30d)")),
        ("With course enrolment", metric("enrolled_flag", "SUM", "Enrolled")),
    )
    specs = [
        ChartSpec(name, big_number(dataset_id, definition, name), 2)
        for name, definition in kpis
    ]
    specs += [
        ChartSpec("Users by role", pie_chart(dataset_id, "primary_role", users), 6),
        ChartSpec(
            "New users per month", month_line(dataset_id, "created_month", users), 6
        ),
        ChartSpec(
            "Users by activity status",
            bar_chart(dataset_id, "activity_status", users),
            6,
        ),
        ChartSpec(
            "Users by authentication method",
            bar_chart(dataset_id, "auth_method", users),
            6,
        ),
        ChartSpec(
            "Never logged in by role",
            bar_chart(
                dataset_id,
                "primary_role",
                metric("never_logged_in_flag", "SUM", "Never logged in"),
            ),
            6,
        ),
        ChartSpec(
            "Users by account status",
            pie_chart(dataset_id, "account_status", users),
            6,
        ),
        ChartSpec("User detail", table_chart(dataset_id, list(DETAIL_COLUMNS)), 12),
    ]
    return specs


def rows_of(charts: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group charts into rows of at most twelve grid columns, in order."""
    rows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    used = 0
    for chart in charts:
        if current and used + chart["width"] > 12:
            rows.append(current)
            current, used = [], 0
        current.append(chart)
        used += chart["width"]
    if current:
        rows.append(current)
    return rows


def layout_for(charts: list[dict[str, Any]]) -> dict[str, Any]:
    """Lay out KPI tiles six per row and the remaining charts two per row."""
    layout: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "HEADER_ID": {
            "id": "HEADER_ID",
            "meta": {"text": DASHBOARD_TITLE},
            "type": "HEADER",
        },
        "ROOT_ID": {"children": ["GRID_ID"], "id": "ROOT_ID", "type": "ROOT"},
        "GRID_ID": {
            "children": [],
            "id": "GRID_ID",
            "parents": ["ROOT_ID"],
            "type": "GRID",
        },
    }
    for chart in charts:
        component_id = f"CHART-{chart['id']}"
        layout[component_id] = {
            "children": [],
            "id": component_id,
            "meta": {
                "chartId": chart["id"],
                "height": (
                    26
                    if chart["width"] == 2
                    else (60 if chart["viz_type"] == "table" else 44)
                ),
                "sliceName": chart["name"],
                "width": chart["width"],
            },
            "parents": ["ROOT_ID", "GRID_ID"],
            "type": "CHART",
        }
    for row_number, row in enumerate(rows_of(charts)):
        row_id = f"ROW-{row_number:02d}"
        layout[row_id] = {
            "children": [f"CHART-{chart['id']}" for chart in row],
            "id": row_id,
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
            "parents": ["ROOT_ID", "GRID_ID"],
            "type": "ROW",
        }
        for chart in row:
            layout[f"CHART-{chart['id']}"]["parents"].append(row_id)
        layout["GRID_ID"]["children"].append(row_id)
    return layout


def native_filters(dataset_id: int) -> list[dict[str, Any]]:
    """Return the dashboard-wide interactive filters."""
    filters = (
        ("Signup date", "filter_time", "created_date"),
        ("Role", "filter_select", "primary_role"),
        ("Activity", "filter_select", "activity_status"),
        ("Account status", "filter_select", "account_status"),
        ("Auth method", "filter_select", "auth_method"),
    )
    result = []
    for index, (name, filter_type, column) in enumerate(filters, 1):
        result.append(
            {
                "cascadeParentIds": [],
                "controlValues": (
                    {}
                    if filter_type == "filter_time"
                    else {
                        "defaultToFirstItem": False,
                        "enableEmptyFilter": False,
                        "inverseSelection": False,
                        "multiSelect": True,
                        "searchAllOptions": True,
                    }
                ),
                "defaultDataMask": {
                    "extraFormData": {},
                    "filterState": {"value": None},
                },
                "description": "",
                "filterType": filter_type,
                "id": f"NATIVE_FILTER-USER-{index}",
                "name": name,
                "scope": {"excluded": [], "rootPath": ["ROOT_ID"]},
                "targets": [{"column": {"name": column}, "datasetId": dataset_id}],
            }
        )
    return result


def resolve_database(api: SupersetApi, name: str) -> dict[str, Any]:
    """Return the connection id and its default schema for a connection name."""
    for database in api.list_all("database"):
        if database.get("database_name") == name:
            connection = api.get(f"/api/v1/database/{database['id']}/connection")
            parameters = connection["result"].get("parameters") or {}
            return {
                "id": int(database["id"]),
                "schema": parameters.get("database") or name,
            }
    raise RuntimeError(f"No Superset connection named {name!r}")


def cleanup(api: SupersetApi, dataset_name: str) -> None:
    """Remove only the artifacts this script owns."""
    for dashboard in api.list_all("dashboard"):
        if dashboard.get("slug") == DASHBOARD_SLUG:
            api.delete(f"/api/v1/dashboard/{dashboard['id']}")
            print(f"Removed dashboard {DASHBOARD_SLUG}")
    for chart in api.list_all("chart"):
        if MARKER in (chart.get("description") or ""):
            api.delete(f"/api/v1/chart/{chart['id']}")
            print(f"Removed chart {chart.get('slice_name')}")
    for dataset in api.list_all("dataset"):
        if dataset.get("table_name") == dataset_name:
            api.delete(f"/api/v1/dataset/{dataset['id']}")
            print(f"Removed dataset {dataset_name}")


def create_dataset(
    api: SupersetApi, database_id: int, schema: str, name: str
) -> int:
    """Create the curated virtual dataset from the report SQL."""
    sql = "\n".join(line.rstrip() for line in DATASET_SQL.strip().splitlines())
    result = api.post(
        "/api/v1/dataset/",
        json={
            "database": database_id,
            "schema": schema,
            "sql": sql,
            "table_name": name,
        },
    )
    print(f"Created dataset {name} (id={result['id']})")
    return int(result["id"])


def create_dashboard(api: SupersetApi) -> int:
    """Create the published dashboard shell that charts attach to."""
    result = api.post(
        "/api/v1/dashboard/",
        json={
            "dashboard_title": DASHBOARD_TITLE,
            "json_metadata": json.dumps({"timed_refresh_immune_slices": []}),
            "position_json": json.dumps({}),
            "published": True,
            "slug": DASHBOARD_SLUG,
        },
    )
    return int(result["id"])


def create_charts(
    api: SupersetApi,
    dataset_id: int,
    dashboard_id: int,
    specs: list[ChartSpec],
) -> list[dict[str, Any]]:
    """Create every chart in the report on the given dashboard."""
    created = []
    for spec in specs:
        response = api.post(
            "/api/v1/chart/",
            json={
                "dashboards": [dashboard_id],
                "datasource_id": dataset_id,
                "datasource_type": "table",
                "description": f"{MARKER}. {spec.name}.",
                "params": json.dumps(spec.form),
                "slice_name": spec.name,
                "viz_type": spec.viz_type,
            },
        )
        created.append(
            {
                "id": int(response["id"]),
                "name": spec.name,
                "viz_type": spec.viz_type,
                "width": spec.width,
            }
        )
        print(f"Created chart {spec.name}")
    return created


def publish_layout(
    api: SupersetApi,
    dashboard_id: int,
    dataset_id: int,
    charts: list[dict[str, Any]],
) -> None:
    """Apply the grid layout and native filters to the dashboard."""
    metadata = {
        "color_scheme": "supersetColors",
        "cross_filters_enabled": True,
        "default_filters": "{}",
        "expanded_slices": {},
        "native_filter_configuration": native_filters(dataset_id),
        "refresh_frequency": 0,
        "timed_refresh_immune_slices": [],
    }
    api.put(
        f"/api/v1/dashboard/{dashboard_id}",
        {
            "dashboard_title": DASHBOARD_TITLE,
            "json_metadata": json.dumps(metadata),
            "position_json": json.dumps(layout_for(charts)),
            "published": True,
            "slug": DASHBOARD_SLUG,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--base-url", help="Superset base URL (default: the .env port)")
    parser.add_argument("--username", help="Superset admin username")
    parser.add_argument("--password", help="Superset admin password")
    parser.add_argument(
        "--database",
        default="kefuat",
        help="Existing Superset connection name holding the Moodle tables",
    )
    parser.add_argument("--dataset-name", default=DATASET_NAME)
    parser.add_argument("--schema", help="Override the schema of the connection")
    args = parser.parse_args()

    env: dict[str, str] = {}
    if args.env_file.exists():
        env = load_env(args.env_file.resolve())
    base_url = args.base_url or f"http://127.0.0.1:{env.get('XPBUILDER_HOST_PORT', 8088)}"
    username = args.username or env.get("SUPERSET_ADMIN_USERNAME")
    password = args.password or env.get("SUPERSET_ADMIN_PASSWORD")
    if not username or not password:
        raise SystemExit(
            "Provide --username/--password or a .env containing "
            "SUPERSET_ADMIN_USERNAME and SUPERSET_ADMIN_PASSWORD"
        )

    api = SupersetApi(base_url, username, password)
    database = resolve_database(api, args.database)
    schema = args.schema or database["schema"]
    cleanup(api, args.dataset_name)
    dataset_id = create_dataset(api, database["id"], schema, args.dataset_name)
    dashboard_id = create_dashboard(api)
    charts = create_charts(api, dataset_id, dashboard_id, chart_specs(dataset_id))
    publish_layout(api, dashboard_id, dataset_id, charts)
    print(
        json.dumps(
            {
                "charts": len(charts),
                "dashboard": f"{base_url}/superset/dashboard/{DASHBOARD_SLUG}/",
                "dataset": args.dataset_name,
                "schema": schema,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
