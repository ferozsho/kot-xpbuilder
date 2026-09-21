#!/usr/bin/env python3
"""Provision the labeled education demo through the XPBuilder HTTP API.

The script is intentionally idempotent. It replaces only the six education
demo upload tables and recreates artifacts marked with the demo slugs/names.
Credentials are read exclusively from the selected ``.env`` file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import requests


DEMO_MARKER = "XPBuilder synthetic education demo"
RAW_TABLES = (
    "students",
    "teachers",
    "courses",
    "enrollments",
    "course_progress",
    "assessment_results",
)
DATASET_SQL = {
    "Student Overview": """
        SELECT student_id, student_name, enrollment_date, enrolled_courses,
               course_progress, completion_status, assessment_score,
               last_activity_date, data_origin
        FROM public.students
    """,
    "Teacher Overview": """
        SELECT teacher_id, teacher_name, assigned_courses,
               assigned_course_count, total_students,
               student_completion_count, student_completion_percentage,
               average_course_performance, data_origin
        FROM public.teachers
    """,
    "Course Analytics": """
        SELECT course_id, course_name, category, assigned_teacher_id,
               enrollment_count, completion_count, completion_percentage,
               average_grade, course_status, data_origin
        FROM public.courses
    """,
    "Enrollment Analytics": """
        SELECT e.enrollment_id, e.student_id, s.student_name,
               CASE
                   WHEN s.completion_status = 'Completed' THEN 'Inactive'
                   ELSE 'Active'
               END AS student_status,
               e.course_id, c.course_name, c.category,
               c.assigned_teacher_id AS teacher_id, t.teacher_name,
               CAST(e.enrollment_date AS date) AS enrollment_date,
               CAST(DATE_TRUNC('month', CAST(e.enrollment_date AS date)) AS date)
                   AS enrollment_month,
               e.enrollment_status,
               p.progress_percent, p.completion_status,
               CAST(p.last_activity_date AS date) AS last_activity_date,
               a.assessment_score,
               CASE WHEN p.completion_status = 'Completed' THEN 1 ELSE 0 END
                   AS completed_flag,
               CASE WHEN s.completion_status = 'Active' THEN 1 ELSE 0 END
                   AS active_student_flag,
               CASE
                   WHEN p.progress_percent < 40 THEN '0-39%'
                   WHEN p.progress_percent < 70 THEN '40-69%'
                   WHEN p.progress_percent < 100 THEN '70-99%'
                   ELSE '100%'
               END AS progress_band,
               CASE
                   WHEN a.assessment_score < 60 THEN 'Below 60'
                   WHEN a.assessment_score < 75 THEN '60-74'
                   WHEN a.assessment_score < 90 THEN '75-89'
                   ELSE '90-100'
               END AS score_band,
               e.data_origin
        FROM public.enrollments e
        JOIN public.students s ON s.student_id = e.student_id
        JOIN public.courses c ON c.course_id = e.course_id
        JOIN public.teachers t ON t.teacher_id = c.assigned_teacher_id
        JOIN public.course_progress p ON p.enrollment_id = e.enrollment_id
        JOIN (
            SELECT enrollment_id, AVG(score) AS assessment_score
            FROM public.assessment_results
            GROUP BY enrollment_id
        ) a ON a.enrollment_id = e.enrollment_id
    """,
    "Learning Progress": """
        SELECT p.progress_id, p.enrollment_id, p.student_id, s.student_name,
               p.course_id, c.course_name, c.category,
               c.assigned_teacher_id AS teacher_id, t.teacher_name,
               p.progress_percent, p.completion_status,
               p.last_activity_date, p.data_origin
        FROM public.course_progress p
        JOIN public.students s ON s.student_id = p.student_id
        JOIN public.courses c ON c.course_id = p.course_id
        JOIN public.teachers t ON t.teacher_id = c.assigned_teacher_id
    """,
    "Assessment Analytics": """
        SELECT a.assessment_result_id, a.enrollment_id, a.student_id,
               s.student_name, a.course_id, c.course_name, c.category,
               c.assigned_teacher_id AS teacher_id, t.teacher_name,
               a.assessment_name, a.score, a.max_score,
               a.assessment_date, a.result_status, a.data_origin
        FROM public.assessment_results a
        JOIN public.students s ON s.student_id = a.student_id
        JOIN public.courses c ON c.course_id = a.course_id
        JOIN public.teachers t ON t.teacher_id = c.assigned_teacher_id
    """,
}

DASHBOARDS = (
    ("Executive Overview", "education-demo-executive-overview"),
    ("Student Analytics", "education-demo-student-analytics"),
    ("Teacher Analytics", "education-demo-teacher-analytics"),
    ("Course Analytics", "education-demo-course-analytics"),
)


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
        raise RuntimeError(
            f"{action} failed with HTTP {response.status_code}: {detail}"
        )

    def get(self, path: str) -> dict[str, Any]:
        response = self.session.get(f"{self.base_url}{path}", timeout=60)
        return self._check(response, f"GET {path}").json()

    def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.post(f"{self.base_url}{path}", timeout=120, **kwargs)
        return self._check(response, f"POST {path}").json()

    def put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.put(f"{self.base_url}{path}", json=payload, timeout=120)
        return self._check(response, f"PUT {path}").json()

    def delete(self, path: str) -> None:
        response = self.session.delete(f"{self.base_url}{path}", timeout=60)
        self._check(response, f"DELETE {path}")

    def list_all(self, resource: str) -> list[dict[str, Any]]:
        result = self.get(f"/api/v1/{resource}/?q=(page:0,page_size:1000)")
        return result.get("result", [])


def load_env(path: Path) -> dict[str, str]:
    """Load the deployment environment without accepting alternate env names."""
    if path.name != ".env":
        raise ValueError("The environment file basename must be exactly .env")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key] = value
    for key in (
        "SUPERSET_ADMIN_USERNAME",
        "SUPERSET_ADMIN_PASSWORD",
        "XPBUILDER_HOST_PORT",
    ):
        if not values.get(key):
            raise ValueError(f"{key} is required in {path}")
    return values


def metric(column: str, aggregate: str, label: str) -> dict[str, Any]:
    """Return a portable simple metric definition."""
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": column},
        "aggregate": aggregate,
        "label": label,
    }


def simple_filter(column: str, value: Any) -> dict[str, Any]:
    """Return an Explore-compatible equality filter."""
    return {
        "clause": "WHERE",
        "comparator": value,
        "expressionType": "SIMPLE",
        "operator": "==",
        "subject": column,
    }


def base_form(dataset_id: int, viz_type: str) -> dict[str, Any]:
    """Return shared, branded chart settings."""
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
    column: str,
    aggregate: str,
    label: str,
    *,
    number_format: str = "SMART_NUMBER",
    filters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    form = base_form(dataset_id, "big_number_total")
    form.update(
        {
            "adhoc_filters": filters or [],
            "header_font_size": 0.42,
            "metric": metric(column, aggregate, label),
            "subheader": label,
            "subheader_font_size": 0.14,
            "y_axis_format": number_format,
        }
    )
    return form


def xy_chart(
    dataset_id: int,
    x_axis: str,
    metric_definition: dict[str, Any],
    *,
    kind: str = "bar",
    groupby: list[str] | None = None,
    temporal: bool = False,
) -> dict[str, Any]:
    viz_type = "echarts_timeseries_line" if kind == "line" else "echarts_timeseries_bar"
    form = base_form(dataset_id, viz_type)
    form.update(
        {
            "groupby": groupby or [],
            "metrics": [metric_definition],
            "rich_tooltip": True,
            "show_value": False,
            "truncate_metric": True,
            "x_axis": x_axis,
            "x_axis_label": x_axis.replace("_", " ").title(),
            "x_axis_label_rotation": 25 if kind == "bar" else 0,
            "x_axis_sort_series": "sum",
            "x_axis_sort_series_ascending": False,
            "y_axis_format": "SMART_NUMBER",
        }
    )
    if temporal:
        form.update(
            {
                "granularity_sqla": x_axis,
                "time_grain_sqla": "P1M",
                "x_axis_time_format": "smart_date",
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
            "show_total": True,
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


def chart_specs(dataset_id: int) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Define every KPI, visualization, and detail table in the brief."""
    students = metric("student_id", "COUNT_DISTINCT", "Total Students")
    courses = metric("course_id", "COUNT_DISTINCT", "Total Courses")
    enrollments = metric("enrollment_id", "COUNT_DISTINCT", "Total Enrollments")
    completed = metric("completed_flag", "SUM", "Completed Enrollments")
    avg_progress = metric("progress_percent", "AVG", "Average Progress")
    avg_score = metric("assessment_score", "AVG", "Average Assessment Score")

    return {
        "Executive Overview": [
            (
                "Total Students",
                big_number(
                    dataset_id, "student_id", "COUNT_DISTINCT", "Total Students"
                ),
            ),
            (
                "Total Teachers",
                big_number(
                    dataset_id, "teacher_id", "COUNT_DISTINCT", "Total Teachers"
                ),
            ),
            (
                "Total Courses",
                big_number(dataset_id, "course_id", "COUNT_DISTINCT", "Total Courses"),
            ),
            (
                "Total Enrollments",
                big_number(
                    dataset_id, "enrollment_id", "COUNT_DISTINCT", "Total Enrollments"
                ),
            ),
            (
                "Active Students",
                big_number(
                    dataset_id,
                    "student_id",
                    "COUNT_DISTINCT",
                    "Active Students",
                    filters=[simple_filter("student_status", "Active")],
                ),
            ),
            (
                "Course Completion Rate",
                big_number(
                    dataset_id,
                    "completed_flag",
                    "AVG",
                    "Course Completion Rate",
                    number_format=".1%",
                ),
            ),
            (
                "Student Enrollment Trends",
                xy_chart(
                    dataset_id,
                    "enrollment_month",
                    enrollments,
                    kind="line",
                    temporal=True,
                ),
            ),
            (
                "Course Completion Trends",
                xy_chart(
                    dataset_id,
                    "enrollment_month",
                    completed,
                    kind="line",
                    temporal=True,
                ),
            ),
            ("Students by Course Category", xy_chart(dataset_id, "category", students)),
            (
                "Course Performance Comparison",
                xy_chart(dataset_id, "course_name", avg_score),
            ),
            (
                "Active versus Inactive Students",
                pie_chart(dataset_id, "student_status", students),
            ),
        ],
        "Student Analytics": [
            (
                "Total Students",
                big_number(
                    dataset_id, "student_id", "COUNT_DISTINCT", "Total Students"
                ),
            ),
            (
                "Active Students",
                big_number(
                    dataset_id,
                    "student_id",
                    "COUNT_DISTINCT",
                    "Active Students",
                    filters=[simple_filter("student_status", "Active")],
                ),
            ),
            (
                "Completed Courses",
                big_number(dataset_id, "completed_flag", "SUM", "Completed Courses"),
            ),
            (
                "Average Progress",
                big_number(
                    dataset_id,
                    "progress_percent",
                    "AVG",
                    "Average Progress",
                    number_format=".1f",
                ),
            ),
            (
                "Average Assessment Score",
                big_number(
                    dataset_id,
                    "assessment_score",
                    "AVG",
                    "Average Assessment Score",
                    number_format=".1f",
                ),
            ),
            (
                "Student Progress Distribution",
                xy_chart(dataset_id, "progress_band", enrollments),
            ),
            (
                "Enrollment Trends",
                xy_chart(
                    dataset_id,
                    "enrollment_month",
                    enrollments,
                    kind="line",
                    temporal=True,
                ),
            ),
            (
                "Course Completion Status",
                pie_chart(dataset_id, "completion_status", enrollments),
            ),
            (
                "Assessment Score Distribution",
                xy_chart(dataset_id, "score_band", enrollments),
            ),
            (
                "Top Courses by Enrollment",
                xy_chart(dataset_id, "course_name", enrollments),
            ),
            (
                "Student Performance Detail",
                table_chart(
                    dataset_id,
                    [
                        "student_id",
                        "student_name",
                        "course_name",
                        "progress_percent",
                        "completion_status",
                        "assessment_score",
                        "last_activity_date",
                    ],
                ),
            ),
        ],
        "Teacher Analytics": [
            (
                "Total Teachers",
                big_number(
                    dataset_id, "teacher_id", "COUNT_DISTINCT", "Total Teachers"
                ),
            ),
            (
                "Assigned Courses",
                big_number(
                    dataset_id, "course_id", "COUNT_DISTINCT", "Assigned Courses"
                ),
            ),
            (
                "Total Students",
                big_number(
                    dataset_id, "student_id", "COUNT_DISTINCT", "Total Students"
                ),
            ),
            (
                "Average Course Completion",
                big_number(
                    dataset_id,
                    "completed_flag",
                    "AVG",
                    "Average Course Completion",
                    number_format=".1%",
                ),
            ),
            ("Students per Teacher", xy_chart(dataset_id, "teacher_name", students)),
            (
                "Courses Assigned per Teacher",
                xy_chart(dataset_id, "teacher_name", courses),
            ),
            (
                "Course Completion by Teacher",
                xy_chart(dataset_id, "teacher_name", completed),
            ),
            (
                "Average Student Performance by Teacher",
                xy_chart(dataset_id, "teacher_name", avg_score),
            ),
            (
                "Teacher Course Performance Detail",
                table_chart(
                    dataset_id,
                    [
                        "teacher_id",
                        "teacher_name",
                        "course_id",
                        "course_name",
                        "student_id",
                        "completion_status",
                        "assessment_score",
                    ],
                ),
            ),
        ],
        "Course Analytics": [
            (
                "Total Courses",
                big_number(dataset_id, "course_id", "COUNT_DISTINCT", "Total Courses"),
            ),
            (
                "Total Enrollments",
                big_number(
                    dataset_id, "enrollment_id", "COUNT_DISTINCT", "Total Enrollments"
                ),
            ),
            (
                "Completed Enrollments",
                big_number(
                    dataset_id, "completed_flag", "SUM", "Completed Enrollments"
                ),
            ),
            (
                "Average Course Progress",
                big_number(
                    dataset_id,
                    "progress_percent",
                    "AVG",
                    "Average Course Progress",
                    number_format=".1f",
                ),
            ),
            (
                "Average Course Grade",
                big_number(
                    dataset_id,
                    "assessment_score",
                    "AVG",
                    "Average Course Grade",
                    number_format=".1f",
                ),
            ),
            ("Most Enrolled Courses", xy_chart(dataset_id, "course_name", enrollments)),
            (
                "Course Completion Rates",
                xy_chart(
                    dataset_id,
                    "course_name",
                    metric("completed_flag", "AVG", "Completion Rate"),
                ),
            ),
            (
                "Student Progress by Course",
                xy_chart(dataset_id, "course_name", avg_progress),
            ),
            ("Enrollment Distribution", pie_chart(dataset_id, "category", enrollments)),
            (
                "Course Performance Trends",
                xy_chart(
                    dataset_id,
                    "enrollment_month",
                    avg_score,
                    kind="line",
                    groupby=["course_name"],
                    temporal=True,
                ),
            ),
        ],
    }


def cleanup_demo(api: SupersetApi) -> None:
    """Remove only artifacts created by previous runs of this script."""
    demo_slugs = {slug for _, slug in DASHBOARDS}
    for dashboard in api.list_all("dashboard"):
        if dashboard.get("slug") in demo_slugs:
            api.delete(f"/api/v1/dashboard/{dashboard['id']}")
    for chart in api.list_all("chart"):
        if str(chart.get("description", "")).startswith(DEMO_MARKER):
            api.delete(f"/api/v1/chart/{chart['id']}")
    for dataset in api.list_all("dataset"):
        if dataset.get("table_name") in DATASET_SQL:
            api.delete(f"/api/v1/dataset/{dataset['id']}")


def upload_csvs(api: SupersetApi, data_dir: Path) -> int:
    """Upload all six files via the same API used by the Superset UI."""
    databases = api.list_all("database")
    upload_databases = [item for item in databases if item.get("allow_file_upload")]
    if len(upload_databases) != 1:
        raise RuntimeError(
            f"Expected one upload-capable database, found {len(upload_databases)}"
        )
    database_id = int(upload_databases[0]["id"])
    for table_name in RAW_TABLES:
        file_path = data_dir / f"{table_name}.csv"
        if not file_path.is_file():
            raise FileNotFoundError(file_path)
        with file_path.open("rb") as handle:
            result = api.post(
                f"/api/v1/database/{database_id}/upload/",
                data={
                    "already_exists": "replace",
                    "dataframe_index": "false",
                    "schema": "public",
                    "skip_blank_lines": "true",
                    "skip_initial_space": "true",
                    "table_name": table_name,
                    "type": "csv",
                },
                files={"file": (file_path.name, handle, "text/csv")},
            )
        if result.get("message") != "OK":
            raise RuntimeError(
                f"Unexpected upload response for {file_path.name}: {result}"
            )
        print(f"Uploaded {file_path.name}")
    return database_id


def create_datasets(api: SupersetApi, database_id: int) -> dict[str, int]:
    """Create the six requested, customer-facing virtual datasets."""
    ids: dict[str, int] = {}
    for name, sql in DATASET_SQL.items():
        result = api.post(
            "/api/v1/dataset/",
            json={
                "database": database_id,
                "schema": "public",
                "sql": "\n".join(line.rstrip() for line in sql.strip().splitlines()),
                "table_name": name,
            },
        )
        ids[name] = int(result["id"])
        print(f"Created dataset {name} (id={ids[name]})")

    # Uploads create raw physical datasets automatically. Keep the tables but
    # remove those duplicate catalog entries so users see the six curated ones.
    for dataset in api.list_all("dataset"):
        if dataset.get("table_name") in RAW_TABLES:
            api.delete(f"/api/v1/dataset/{dataset['id']}")
    return ids


def dashboard_css() -> str:
    """Return compact responsive styling scoped to the demo dashboards."""
    return """
.dashboard-content { background: #f5f7fb; padding: 18px; }
.dashboard-component-chart-holder {
  background: #ffffff; border: 1px solid #e7ebf2; border-radius: 14px;
  box-shadow: 0 5px 18px rgba(28, 39, 76, 0.08); overflow: hidden;
}
.dashboard-component-header { color: #182230; font-weight: 700; }
.chart-header { padding: 10px 14px 0; }
.grid-row { margin-bottom: 10px; }
.demo-banner {
  background: linear-gradient(90deg, #e8f7fb, #fff7e8); border-radius: 10px;
  color: #344054; font-size: 13px; padding: 10px 14px;
}
@media (max-width: 768px) {
  .dashboard-content { padding: 8px; }
  .dashboard-component-chart-holder { border-radius: 10px; }
  #main-menu { height: 64px !important; overflow: hidden !important; }
  #main-menu > .ant-row {
    height: 64px !important; min-height: 64px !important;
    flex-wrap: nowrap !important;
  }
  #main-menu .main-nav { display: none !important; }
  main.ant-layout-content > div {
    grid-template-columns: minmax(0, 1fr) !important;
  }
  main.ant-layout-content > div > div:nth-child(-n + 2) {
    display: none !important;
  }
  main.ant-layout-content > div > div:nth-child(n + 3) {
    grid-column: 1 !important; width: 100% !important;
  }
  .dashboard-header-container { display: none !important; }
  .dashboard, .dashboard-content, .grid-container {
    left: 0 !important; max-width: 100% !important; width: 100% !important;
  }
  .grid-row {
    display: flex !important; flex-direction: column !important;
    gap: 12px !important; height: auto !important;
  }
  .grid-row > div, .grid-row .resizable-container,
  .grid-row .dashboard-component-chart-holder {
    flex: 0 0 auto !important; max-width: 100% !important; width: 100% !important;
  }
  .big_number_total .header-line {
    font-size: 42px !important; line-height: 1.1 !important;
  }
}
""".strip()


def create_dashboard_shells(api: SupersetApi) -> dict[str, dict[str, Any]]:
    """Create published dashboard records before attaching charts."""
    dashboards: dict[str, dict[str, Any]] = {}
    for title, slug in DASHBOARDS:
        result = api.post(
            "/api/v1/dashboard/",
            json={
                "css": dashboard_css(),
                "dashboard_title": title,
                "json_metadata": json.dumps({"timed_refresh_immune_slices": []}),
                "position_json": json.dumps({}),
                "published": True,
                "slug": slug,
            },
        )
        dashboards[title] = {"id": int(result["id"]), "slug": slug}
    return dashboards


def layout_for(title: str, charts: list[dict[str, Any]]) -> dict[str, Any]:
    """Create a responsive 12-column dashboard layout."""
    layout: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "HEADER_ID": {"id": "HEADER_ID", "meta": {"text": title}, "type": "HEADER"},
        "ROOT_ID": {"children": ["GRID_ID"], "id": "ROOT_ID", "type": "ROOT"},
        "GRID_ID": {
            "children": ["ROW-DEMO-NOTICE"],
            "id": "GRID_ID",
            "parents": ["ROOT_ID"],
            "type": "GRID",
        },
        "MARKDOWN-DEMO-NOTICE": {
            "children": [],
            "id": "MARKDOWN-DEMO-NOTICE",
            "meta": {
                "code": (
                    f'<div class="demo-banner"><strong>{title}</strong><br>'
                    "Synthetic demo data - realistic, relationship-safe education "
                    "records for product demonstration; no production learner data "
                    "is shown.</div>"
                ),
                "height": 10,
                "width": 12,
            },
            "parents": ["ROOT_ID", "GRID_ID", "ROW-DEMO-NOTICE"],
            "type": "MARKDOWN",
        },
        "ROW-DEMO-NOTICE": {
            "children": ["MARKDOWN-DEMO-NOTICE"],
            "id": "ROW-DEMO-NOTICE",
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
            "parents": ["ROOT_ID", "GRID_ID"],
            "type": "ROW",
        },
    }
    chart_component_ids = []
    for index, chart in enumerate(charts):
        component_id = f"CHART-{chart['id']}"
        chart_component_ids.append(component_id)
        layout[component_id] = {
            "children": [],
            "id": component_id,
            "meta": {
                "chartId": chart["id"],
                "height": 26
                if index < chart["kpi_count"]
                else (56 if chart["viz_type"] == "table" else 44),
                "sliceName": chart["name"],
                "width": chart["width"],
            },
            "parents": ["ROOT_ID", "GRID_ID"],
            "type": "CHART",
        }

    cursor = 0
    row_number = 0
    while cursor < len(chart_component_ids):
        kpi_count = charts[0]["kpi_count"]
        row_size = min(kpi_count, 6) if cursor < kpi_count else 2
        row_children = chart_component_ids[cursor : cursor + row_size]
        row_id = f"ROW-{row_number:02d}"
        layout[row_id] = {
            "children": row_children,
            "id": row_id,
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
            "parents": ["ROOT_ID", "GRID_ID"],
            "type": "ROW",
        }
        for component_id in row_children:
            layout[component_id]["parents"].append(row_id)
        layout["GRID_ID"]["children"].append(row_id)
        cursor += row_size
        row_number += 1
    return layout


def native_filters(dataset_id: int) -> list[dict[str, Any]]:
    """Create the five requested dashboard-wide interactive filters."""
    filters = (
        ("Date range", "filter_time", "enrollment_date"),
        ("Course", "filter_select", "course_name"),
        ("Teacher", "filter_select", "teacher_name"),
        ("Student status", "filter_select", "student_status"),
        ("Course category", "filter_select", "category"),
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
                "id": f"NATIVE_FILTER-EDU-{index}",
                "name": name,
                "scope": {"excluded": [], "rootPath": ["ROOT_ID"]},
                "targets": [{"column": {"name": column}, "datasetId": dataset_id}],
            }
        )
    return result


def create_charts_and_layouts(
    api: SupersetApi,
    dataset_id: int,
    dashboards: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create all requested charts and apply dashboard layout/filter metadata."""
    created: list[dict[str, Any]] = []
    for title, specs in chart_specs(dataset_id).items():
        dashboard = dashboards[title]
        kpi_count = next(
            index
            for index, (_, form) in enumerate(specs)
            if form["viz_type"] != "big_number_total"
        )
        dashboard_charts = []
        for index, (chart_name, form_data) in enumerate(specs):
            full_name = chart_name
            response = api.post(
                "/api/v1/chart/",
                json={
                    "dashboards": [dashboard["id"]],
                    "datasource_id": dataset_id,
                    "datasource_type": "table",
                    "description": f"{DEMO_MARKER}. {chart_name}.",
                    "params": json.dumps(form_data),
                    "slice_name": full_name,
                    "viz_type": form_data["viz_type"],
                },
            )
            chart = {
                "dashboard": title,
                "id": int(response["id"]),
                "kpi_count": kpi_count,
                "name": full_name,
                "viz_type": form_data["viz_type"],
                "width": (
                    12
                    if form_data["viz_type"] == "table"
                    else (12 // min(kpi_count, 6) if index < kpi_count else 6)
                ),
            }
            dashboard_charts.append(chart)
            created.append(chart)

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
            f"/api/v1/dashboard/{dashboard['id']}",
            {
                "css": dashboard_css(),
                "dashboard_title": title,
                "json_metadata": json.dumps(metadata),
                "position_json": json.dumps(layout_for(title, dashboard_charts)),
                "published": True,
                "slug": dashboard["slug"],
            },
        )
        print(f"Created dashboard {title} with {len(dashboard_charts)} charts")
    return created


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--base-url")
    args = parser.parse_args()

    env = load_env(args.env_file.resolve())
    base_url = args.base_url or f"http://127.0.0.1:{env['XPBUILDER_HOST_PORT']}"
    data_dir = Path(__file__).resolve().parent / "data"
    api = SupersetApi(
        base_url,
        env["SUPERSET_ADMIN_USERNAME"],
        env["SUPERSET_ADMIN_PASSWORD"],
    )

    cleanup_demo(api)
    database_id = upload_csvs(api, data_dir)
    dataset_ids = create_datasets(api, database_id)
    dashboards = create_dashboard_shells(api)
    charts = create_charts_and_layouts(
        api, dataset_ids["Enrollment Analytics"], dashboards
    )
    print(
        json.dumps(
            {
                "charts": len(charts),
                "dashboards": {
                    title: f"{base_url}/superset/dashboard/{details['slug']}/"
                    for title, details in dashboards.items()
                },
                "datasets": dataset_ids,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
