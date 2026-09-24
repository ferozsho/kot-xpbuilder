#!/usr/bin/env python3
"""Provision the training-programme dashboards on a live Moodle database.

Rebuilds the client's three Power BI pages -- Teacher, Headmaster and Education
Officer -- as three Superset dashboards over one read-only virtual dataset, so
every tile is aggregated live from the connected Moodle database and updates as
the data changes. Nothing is written to the connected database: the dataset is
defined as SQL, and no table is created, altered or dropped.

Live mapping (the client's flat "Program Data" export does not exist in the
Moodle database, so each column is derived from the closest live equivalent):

======================  ====================================================
Report column           Live source
======================  ====================================================
Teacher / participant    user holding a programme role (``employee``,
                        ``editingteacher``, ``trainer``, ``teacher``)
                        in a programme course
Schools / district       ``mdl_local_school`` through ``mdl_user.open_school``
Courses assigned         distinct programme courses the user is a member of
Courses completed        ``mdl_course_completions`` rows for those courses
Artefacts submitted      submissions of artefact activities (activity or
                        course named *artefact/artifact*, or an ``ART``
                        course shortname)
Average artefact score   ``mdl_assign_grades`` as a percentage of the activity
                        maximum
Microassessment score    graded ``Micro Assessment - <concept>`` activities,
                        grouped by the concept after the dash
Classroom observations   ``mdl_local_observations_users`` records plus
                        *classroom observation* activity submissions
BL / ML scores           baseline items (*baseline*, *pre test*, *pre course*,
                        *pre survey*) and post items (*post*), as percentages
Student gain %           (average post score - average baseline score) / average
                        baseline score
======================  ====================================================

The Headmaster and Education Officer pages are reported at school and district
level, because the Moodle database holds no education-officer or headmaster
records; the pages keep the client's titles so they can be compared side by
side with the Power BI originals.

```bash
python3 reports/provision_programme_dashboards.py \
    --base-url https://kot5.example.com \
    --username admin --password '<admin password>' \
    --connection kefuat
```

Without ``--base-url`` the script reads ``XPBUILDER_HOST_PORT``,
``SUPERSET_ADMIN_USERNAME`` and ``SUPERSET_ADMIN_PASSWORD`` from ``--env-file``
(default ``./.env``). The script is idempotent: only the dataset, charts and
dashboards marked with ``MARKER`` or its slugs are replaced.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

MARKER = "XPBuilder training programme report"
DATASET_NAME = "Programme Activity Report"
DEFAULT_CONNECTION = "kefuat"

DASHBOARD_SLUGS = (
    "programme-teacher-report",
    "programme-headmaster-report",
    "programme-education-officer-report",
)

# One row per (programme participant x programme course x activity). The grain
# keeps every level of the report filterable: native filters on the participant,
# the school, the district, the course and the course category all narrow the
# same dataset, and the activity charts add their own activity_kind filter.
DATASET_SQL = """
WITH programme_courses AS (
    SELECT c.id AS course_id,
           c.fullname AS course_name,
           c.shortname AS course_shortname,
           COALESCE(cc.name, 'Uncategorised') AS course_category
    FROM mdl_course c
    LEFT JOIN mdl_course_categories cc ON cc.id = c.category
    WHERE c.id > 1 AND (
        c.shortname LIKE 'KSH%'
        OR c.shortname LIKE '%TRTI%'
        OR c.shortname LIKE '%FLN%'
        OR c.shortname LIKE '%ART%'
        OR LOWER(c.fullname) LIKE '%trti%'
        OR LOWER(c.fullname) LIKE '%artefact%'
        OR LOWER(c.fullname) LIKE '%kshamata%'
        OR LOWER(c.fullname) LIKE '%ped-tech%'
        OR LOWER(c.fullname) LIKE '%pedtech%'
        OR LOWER(c.fullname) LIKE '%fln%'
    )
),
participants AS (
    SELECT DISTINCT ctx.instanceid AS course_id, ra.userid AS user_id
    FROM mdl_role_assignments ra
    JOIN mdl_role r ON r.id = ra.roleid
    JOIN mdl_context ctx ON ctx.id = ra.contextid AND ctx.contextlevel = 50
    JOIN programme_courses pc ON pc.course_id = ctx.instanceid
    WHERE r.shortname IN ('employee', 'editingteacher', 'trainer', 'teacher')
),
learners AS (
    SELECT e.courseid AS course_id, COUNT(DISTINCT ue.userid) AS course_learners
    FROM mdl_enrol e
    JOIN mdl_user_enrolments ue ON ue.enrolid = e.id
    JOIN programme_courses pc ON pc.course_id = e.courseid
    GROUP BY e.courseid
),
completions AS (
    SELECT DISTINCT cc.course AS course_id, cc.userid AS user_id
    FROM mdl_course_completions cc
    JOIN programme_courses pc ON pc.course_id = cc.course
),
submission_rows AS (
    SELECT s.userid AS user_id, a.course AS course_id,
           CASE
               WHEN LOWER(a.name) LIKE '%micro assessment%' THEN 'micro_assessment'
               WHEN LOWER(a.name) LIKE '%observation%' THEN 'classroom_observation'
               WHEN LOWER(a.name) LIKE '%artefact%'
                   OR LOWER(a.name) LIKE '%artifact%'
                   OR LOWER(pc.course_name) LIKE '%artefact%'
                   OR LOWER(pc.course_name) LIKE '%artifact%'
                   OR UPPER(pc.course_shortname) LIKE '%ART%' THEN 'artefact'
               ELSE 'other_submission'
           END AS activity_kind,
           a.name AS activity_name,
           CASE WHEN LOWER(a.name) LIKE '%micro assessment%'
               THEN TRIM(SUBSTRING_INDEX(a.name, '-', -1)) END AS concept,
           CASE WHEN a.grade > 0 AND ag.grade >= 0
               THEN ROUND(ag.grade / a.grade * 100, 2) END AS score_pct,
           DATE(FROM_UNIXTIME(s.timemodified)) AS scored_on
    FROM mdl_assign_submission s
    JOIN mdl_assign a ON a.id = s.assignment
    JOIN programme_courses pc ON pc.course_id = a.course
    LEFT JOIN mdl_assign_grades ag
        ON ag.assignment = a.id AND ag.userid = s.userid
    WHERE s.status = 'submitted'
),
assessment_rows AS (
    SELECT gg.userid AS user_id, gi.courseid AS course_id,
           CASE
               WHEN LOWER(gi.itemname) LIKE '%micro assessment%' THEN 'micro_assessment'
               WHEN LOWER(gi.itemname) LIKE '%baseline%'
                   OR LOWER(gi.itemname) LIKE '%pre test%'
                   OR LOWER(gi.itemname) LIKE '%pre-test%'
                   OR LOWER(gi.itemname) LIKE '%pre course%'
                   OR LOWER(gi.itemname) LIKE '%pre survey%' THEN 'baseline'
               WHEN LOWER(gi.itemname) LIKE '%post%' THEN 'post_test'
               WHEN LOWER(gi.itemname) LIKE '%observation%'
                   THEN 'classroom_observation'
               WHEN LOWER(gi.itemname) LIKE '%artefact%'
                   OR LOWER(gi.itemname) LIKE '%artifact%' THEN 'artefact'
               ELSE 'other_assessment'
           END AS activity_kind,
           gi.itemname AS activity_name,
           CASE WHEN LOWER(gi.itemname) LIKE '%micro assessment%'
               THEN TRIM(SUBSTRING_INDEX(gi.itemname, '-', -1)) END AS concept,
           CASE WHEN gi.grademax > 0
               THEN ROUND(gg.finalgrade / gi.grademax * 100, 2) END AS score_pct,
           DATE(FROM_UNIXTIME(gg.timemodified)) AS scored_on
    FROM mdl_grade_grades gg
    JOIN mdl_grade_items gi ON gi.id = gg.itemid
    JOIN programme_courses pc ON pc.course_id = gi.courseid
    WHERE gg.finalgrade IS NOT NULL
      AND gg.finalgrade >= 0
      AND gi.itemname IS NOT NULL
      AND gi.itemmodule <> 'assign'
),
observation_rows AS (
    SELECT ou.userid AS user_id, NULL AS course_id,
           'classroom_observation' AS activity_kind,
           o.name AS activity_name, NULL AS concept, NULL AS score_pct,
           DATE(FROM_UNIXTIME(ou.timecreated)) AS scored_on
    FROM mdl_local_observations_users ou
    JOIN mdl_local_observations o ON o.id = ou.observationsid
),
enrolment_rows AS (
    SELECT p.user_id, p.course_id, 'enrolment' AS activity_kind,
           NULL AS activity_name, NULL AS concept, NULL AS score_pct,
           NULL AS scored_on
    FROM participants p
),
union_rows AS (
    SELECT * FROM submission_rows
    UNION ALL SELECT * FROM assessment_rows
    UNION ALL SELECT * FROM observation_rows
    UNION ALL SELECT * FROM enrolment_rows
),
ranked AS (
    SELECT u.*,
           ROW_NUMBER() OVER (
               PARTITION BY u.course_id ORDER BY u.user_id, u.activity_kind
           ) AS course_first_row
    FROM union_rows u
)
SELECT
    r.user_id AS participant_id,
    CONCAT(u.firstname, ' ', u.lastname) AS participant_name,
    u.username AS participant_username,
    NULLIF(NULLIF(u.open_designation, ''), 'NA') AS designation,
    u.open_school AS school_id,
    COALESCE(s.school_name, 'No school set') AS school_name,
    s.open_district AS district_id,
    s.open_tahsil AS taluka_id,
    r.course_id,
    pc.course_name,
    pc.course_shortname,
    pc.course_category,
    CASE WHEN r.activity_kind = 'enrolment' THEN 1 ELSE 0 END AS enrolled_flag,
    CASE WHEN cp.user_id IS NULL THEN 0 ELSE 1 END AS completed_flag,
    CASE WHEN r.course_first_row = 1 THEN COALESCE(l.course_learners, 0) ELSE 0 END
        AS course_learners,
    r.activity_kind,
    r.activity_name,
    r.concept,
    r.score_pct,
    r.scored_on
FROM ranked r
JOIN mdl_user u ON u.id = r.user_id
LEFT JOIN mdl_local_school s ON s.id = u.open_school
LEFT JOIN programme_courses pc ON pc.course_id = r.course_id
LEFT JOIN learners l ON l.course_id = r.course_id
LEFT JOIN completions cp
    ON cp.course_id = r.course_id AND cp.user_id = r.user_id
"""

PERCENT_1 = ".1%"
NUMBER_1 = ",.1f"
NUMBER_0 = ",.0f"

ACTIVITY_COLUMNS = (
    "participant_name",
    "school_name",
    "course_shortname",
    "activity_kind",
    "activity_name",
    "concept",
    "score_pct",
    "scored_on",
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


@dataclass
class DashboardSpec:
    """One report page: its charts and its dashboard-wide filters."""

    title: str
    slug: str
    charts: list[ChartSpec]
    filters: list[tuple[str, str, str | None]]


@dataclass
class Connection:
    """A resolved Superset database connection."""

    id: int
    schema: str


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
        response = self.session.get(f"{self.base_url}{path}", timeout=180)
        return self._check(response, f"GET {path}").json()

    def post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        response = self.session.post(f"{self.base_url}{path}", timeout=300, **kwargs)
        return self._check(response, f"POST {path}").json()

    def put(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self.session.put(f"{self.base_url}{path}", json=payload, timeout=300)
        return self._check(response, f"PUT {path}").json()

    def delete(self, path: str) -> None:
        response = self.session.delete(f"{self.base_url}{path}", timeout=120)
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


def resolve_connection(api: SupersetApi, name: str) -> Connection:
    """Return the connection id and default schema for a connection name."""
    for database in api.list_all("database"):
        if database.get("database_name") == name:
            connection = api.get(f"/api/v1/database/{database['id']}/connection")
            parameters = connection["result"].get("parameters") or {}
            return Connection(
                id=int(database["id"]),
                schema=parameters.get("database") or name,
            )
    raise RuntimeError(f"No Superset connection named {name!r}")


def simple(column: str, aggregate: str, label: str) -> dict[str, Any]:
    """Return a portable simple metric definition."""
    return {
        "aggregate": aggregate,
        "column": {"column_name": column},
        "expressionType": "SIMPLE",
        "label": label,
    }


def sql_metric(expression: str, label: str) -> dict[str, Any]:
    """Return a custom-SQL metric definition."""
    return {"expressionType": "SQL", "label": label, "sqlExpression": expression}


def count_of_kind(kind: str, label: str) -> dict[str, Any]:
    """Return a metric counting the rows of one activity kind."""
    return sql_metric(
        f"SUM(CASE WHEN activity_kind = '{kind}' THEN 1 ELSE 0 END)", label
    )


def score_of_kind(kind: str, label: str) -> dict[str, Any]:
    """Return the average score of one activity kind."""
    return sql_metric(
        f"AVG(CASE WHEN activity_kind = '{kind}' THEN score_pct END)", label
    )


def score_gain_metric(label: str = "Score Gain %") -> dict[str, Any]:
    """Return the baseline-to-post gain, as used by the Power BI cards."""
    baseline = "AVG(CASE WHEN activity_kind = 'baseline' THEN score_pct END)"
    post = "AVG(CASE WHEN activity_kind = 'post_test' THEN score_pct END)"
    return sql_metric(f"({post} - {baseline}) / NULLIF({baseline}, 0)", label)


def kind_filter(*kinds: str) -> dict[str, Any]:
    """Return a chart-level filter on the activity kind."""
    return {
        "clause": "WHERE",
        "comparator": list(kinds),
        "expressionType": "SIMPLE",
        "operator": "IN",
        "subject": "activity_kind",
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


def kpi(
    dataset_id: int,
    metric_definition: dict[str, Any],
    label: str,
    number_format: str = NUMBER_0,
) -> dict[str, Any]:
    """Return a KPI tile."""
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


def bar_chart(
    dataset_id: int,
    x_axis: str,
    metrics: list[dict[str, Any]],
    *,
    number_format: str = NUMBER_1,
    orientation: str = "vertical",
    sort_by_value: bool = False,
    filter_kinds: tuple[str, ...] = (),
    x_axis_label: str | None = None,
    y_axis_label: str | None = None,
) -> dict[str, Any]:
    """Return a clustered bar chart, optionally horizontal and filtered."""
    form = base_form(dataset_id, "echarts_timeseries_bar")
    form.update(
        {
            "metrics": metrics,
            "orientation": orientation,
            "rich_tooltip": True,
            "show_value": False,
            "x_axis": x_axis,
            "x_axis_label_rotation": 25 if orientation == "vertical" else 0,
            "x_axis_title": x_axis_label or x_axis.replace("_", " ").title(),
            "y_axis_format": number_format,
            "y_axis_title": y_axis_label or "",
        }
    )
    if filter_kinds:
        form["adhoc_filters"] = [kind_filter(*filter_kinds)]
    if sort_by_value:
        form.update(
            {
                "x_axis_sort_series": "sum",
                "x_axis_sort_series_ascending": False,
            }
        )
    return form


def area_chart(
    dataset_id: int,
    x_axis: str,
    metric_definition: dict[str, Any],
    *,
    number_format: str = PERCENT_1,
    filter_kinds: tuple[str, ...] = (),
    x_axis_label: str | None = None,
    y_axis_label: str | None = None,
) -> dict[str, Any]:
    """Return a filled area chart, as used by the Power BI line visuals."""
    form = base_form(dataset_id, "echarts_area")
    form.update(
        {
            "metrics": [metric_definition],
            "opacity": 0.35,
            "rich_tooltip": True,
            "show_value": False,
            "x_axis": x_axis,
            "x_axis_label_rotation": 0,
            "x_axis_title": x_axis_label or x_axis.replace("_", " ").title(),
            "y_axis_format": number_format,
            "y_axis_title": y_axis_label or "",
        }
    )
    if filter_kinds:
        form["adhoc_filters"] = [kind_filter(*filter_kinds)]
    return form


def raw_table(
    dataset_id: int,
    columns: tuple[str, ...],
    *,
    filter_kinds: tuple[str, ...] = (),
    page_length: int = 25,
) -> dict[str, Any]:
    """Return a raw detail table."""
    form = base_form(dataset_id, "table")
    form.update(
        {
            "all_columns": list(columns),
            "include_search": True,
            "order_by_cols": [],
            "page_length": page_length,
            "query_mode": "raw",
            "server_page_length": page_length,
            "show_cell_bars": False,
            "table_timestamp_format": "%Y-%m-%d",
        }
    )
    if filter_kinds:
        form["adhoc_filters"] = [kind_filter(*filter_kinds)]
    return form


def scorecard_table(dataset_id: int) -> dict[str, Any]:
    """Return the per-participant scorecard table."""
    form = base_form(dataset_id, "table")
    form.update(
        {
            "groupby": ["participant_name", "designation", "school_name"],
            "include_search": True,
            "metrics": [
                simple("course_id", "COUNT_DISTINCT", "Courses Assigned"),
                sql_metric(
                    "COUNT(DISTINCT CASE WHEN completed_flag = 1 "
                    "THEN course_id END)",
                    "Courses Completed",
                ),
                simple("course_learners", "SUM", "Students"),
                count_of_kind("artefact", "Artefacts Submitted"),
                score_of_kind("artefact", "Avg Artefact Score"),
                count_of_kind("classroom_observation", "Classroom Observations"),
                score_of_kind("baseline", "Baseline Score"),
                score_of_kind("post_test", "Post Score"),
                score_gain_metric("Score Gain %"),
            ],
            "order_desc": True,
            "page_length": 25,
            "query_mode": "aggregate",
            "server_page_length": 25,
            "show_cell_bars": True,
            "table_timestamp_format": "%Y-%m-%d",
        }
    )
    return form


def teacher_report(dataset_id: int) -> list[ChartSpec]:
    """Return the Teacher page, mirroring the client's teacher report."""
    courses_completed = (
        "COUNT(DISTINCT CASE WHEN completed_flag = 1 THEN course_id END)"
    )
    return [
        ChartSpec(
            "Courses Assigned",
            kpi(dataset_id, simple("course_id", "COUNT_DISTINCT",
                                   "Courses Assigned"), "Courses Assigned"),
            3,
        ),
        ChartSpec(
            "Courses Completed",
            kpi(dataset_id, sql_metric(courses_completed, "Courses Completed"),
                "Courses Completed"),
            3,
        ),
        ChartSpec(
            "Courses Completed %",
            kpi(
                dataset_id,
                sql_metric(
                    f"{courses_completed} / NULLIF(COUNT(DISTINCT course_id), 0)",
                    "Courses Completed %",
                ),
                "Courses Completed %",
                PERCENT_1,
            ),
            3,
        ),
        ChartSpec(
            "Student Enrolments",
            kpi(dataset_id,
                simple("course_learners", "SUM", "Student Enrolments"),
                "Student Enrolments"),
            3,
        ),
        ChartSpec(
            "Artefacts Submitted",
            kpi(dataset_id, count_of_kind("artefact", "Artefacts Submitted"),
                "Artefacts Submitted"),
            3,
        ),
        ChartSpec(
            "Avg Artefact Score",
            kpi(dataset_id, score_of_kind("artefact", "Avg Artefact Score"),
                "Avg Artefact Score (%)", NUMBER_1),
            3,
        ),
        ChartSpec(
            "Classroom Observations",
            kpi(dataset_id,
                count_of_kind("classroom_observation", "Classroom Observations"),
                "Classroom Observations"),
            3,
        ),
        ChartSpec(
            "Micro Assessment Score",
            kpi(dataset_id, score_of_kind("micro_assessment",
                                          "Micro Assessment Score"),
                "Micro Assessment Score (%)", NUMBER_1),
            3,
        ),
        ChartSpec(
            "Baseline Score",
            kpi(dataset_id, score_of_kind("baseline", "Baseline Score"),
                "Avg Baseline Score (%)", NUMBER_1),
            3,
        ),
        ChartSpec(
            "Post Score",
            kpi(dataset_id, score_of_kind("post_test", "Post Score"),
                "Avg Post Score (%)", NUMBER_1),
            3,
        ),
        ChartSpec(
            "Score Gain %",
            kpi(dataset_id, score_gain_metric(), "Score Gain %", PERCENT_1),
            3,
        ),
        ChartSpec(
            "Assessments Recorded",
            kpi(dataset_id,
                sql_metric("SUM(CASE WHEN score_pct IS NOT NULL THEN 1 ELSE 0 END)",
                           "Assessments Recorded"),
                "Assessments Recorded"),
            3,
        ),
        ChartSpec(
            "Baseline vs Post Score",
            bar_chart(
                dataset_id,
                "activity_kind",
                [score_of_kind("baseline", "Avg. Baseline Score"),
                 score_of_kind("post_test", "Avg. Post Score")],
                filter_kinds=("baseline", "post_test"),
                x_axis_label="Assessment",
                y_axis_label="Score (%)",
            ),
            6,
        ),
        ChartSpec(
            "Micro Assessment Score by Concept",
            bar_chart(
                dataset_id,
                "concept",
                [score_of_kind("micro_assessment", "Micro Assessment Score")],
                filter_kinds=("micro_assessment",),
                x_axis_label="Microassessment Concept",
                y_axis_label="Score (%)",
            ),
            6,
        ),
        ChartSpec(
            "Score Gain % by Participant",
            area_chart(
                dataset_id,
                "participant_name",
                score_gain_metric(),
                filter_kinds=("baseline", "post_test"),
                x_axis_label="Participant Name",
                y_axis_label="Score Gain %",
            ),
            6,
        ),
        ChartSpec(
            "Artefacts Submitted by Participant",
            bar_chart(
                dataset_id,
                "participant_name",
                [count_of_kind("artefact", "Artefacts Submitted")],
                sort_by_value=True,
                filter_kinds=("artefact",),
                x_axis_label="Participant Name",
                y_axis_label="Artefacts Submitted",
            ),
            6,
        ),
        ChartSpec(
            "Activity Detail",
            raw_table(dataset_id, ACTIVITY_COLUMNS, page_length=50),
            12,
        ),
    ]


def headmaster_report(dataset_id: int) -> list[ChartSpec]:
    """Return the Headmaster page, reported at school level."""
    return [
        ChartSpec(
            "Total Participants",
            kpi(dataset_id, simple("participant_id", "COUNT_DISTINCT",
                                   "Total Participants"), "Total Participants"),
            2,
        ),
        ChartSpec(
            "Total Courses",
            kpi(dataset_id, simple("course_id", "COUNT_DISTINCT", "Total Courses"),
                "Total Course"),
            2,
        ),
        ChartSpec(
            "Student Enrolments",
            kpi(dataset_id,
                simple("course_learners", "SUM", "Student Enrolments"),
                "Student Enrolments"),
            2,
        ),
        ChartSpec(
            "Artefacts Submitted",
            kpi(dataset_id, count_of_kind("artefact", "Artefacts Submitted"),
                "Artefacts Submitted"),
            2,
        ),
        ChartSpec(
            "Avg Artefact Score out of 100",
            kpi(dataset_id, score_of_kind("artefact", "Avg Artefact Score"),
                "Avg Artefact Score (%)", NUMBER_1),
            2,
        ),
        ChartSpec(
            "Classroom Observations",
            kpi(dataset_id,
                count_of_kind("classroom_observation", "Classroom Observations"),
                "Classroom Observations"),
            2,
        ),
        ChartSpec(
            "BL and ML Gain by Participant",
            bar_chart(
                dataset_id,
                "participant_name",
                [score_gain_metric("BL & ML % Gain")],
                number_format=PERCENT_1,
                orientation="horizontal",
                sort_by_value=True,
                filter_kinds=("baseline", "post_test"),
                x_axis_label="Participant Name",
                y_axis_label="BL & ML % Gain",
            ),
            6,
        ),
        ChartSpec(
            "Micro Assessment Score by Concept",
            area_chart(
                dataset_id,
                "concept",
                score_of_kind("micro_assessment", "Micro Assessment Score"),
                number_format=NUMBER_1,
                filter_kinds=("micro_assessment",),
                x_axis_label="Microassessment Concept",
                y_axis_label="Score (%)",
            ),
            6,
        ),
        ChartSpec(
            "Average Artefact Score by Participant",
            bar_chart(
                dataset_id,
                "participant_name",
                [score_of_kind("artefact", "Avg. Artefact Score")],
                sort_by_value=True,
                filter_kinds=("artefact",),
                x_axis_label="Participant Name",
                y_axis_label="Avg Artefact Score (%)",
            ),
            6,
        ),
        ChartSpec(
            "Baseline vs Post Score by Participant",
            bar_chart(
                dataset_id,
                "participant_name",
                [score_of_kind("baseline", "Avg. Baseline Score"),
                 score_of_kind("post_test", "Avg. Post Score")],
                filter_kinds=("baseline", "post_test"),
                x_axis_label="Participant Name",
                y_axis_label="Score (%)",
            ),
            6,
        ),
        ChartSpec("Participant Scorecard", scorecard_table(dataset_id), 12),
        ChartSpec(
            "Activity Detail",
            raw_table(dataset_id, ACTIVITY_COLUMNS, page_length=50),
            12,
        ),
    ]


def education_officer_report(dataset_id: int) -> list[ChartSpec]:
    """Return the Education Officer page, reported at district level."""
    return [
        ChartSpec(
            "Total Schools",
            kpi(dataset_id, simple("school_name", "COUNT_DISTINCT",
                                   "Total Schools"), "Total School"),
            3,
        ),
        ChartSpec(
            "Total Participants",
            kpi(dataset_id, simple("participant_id", "COUNT_DISTINCT",
                                   "Total Participants"), "Teacher / Participant"),
            3,
        ),
        ChartSpec(
            "Total Courses",
            kpi(dataset_id, simple("course_id", "COUNT_DISTINCT", "Total Courses"),
                "Total Courses"),
            3,
        ),
        ChartSpec(
            "Student Enrolments",
            kpi(dataset_id,
                simple("course_learners", "SUM", "Student Enrolments"),
                "Student Enrolments"),
            3,
        ),
        ChartSpec(
            "Micro Assessment Score by Concept",
            area_chart(
                dataset_id,
                "concept",
                score_of_kind("micro_assessment", "Micro Assessment Score"),
                number_format=NUMBER_1,
                filter_kinds=("micro_assessment",),
                x_axis_label="Microassessment Concept",
                y_axis_label="Score (%)",
            ),
            6,
        ),
        ChartSpec(
            "Score Gain % by School",
            area_chart(
                dataset_id,
                "school_name",
                score_gain_metric(),
                filter_kinds=("baseline", "post_test"),
                x_axis_label="School Name",
                y_axis_label="Score Gain %",
            ),
            6,
        ),
        ChartSpec(
            "Baseline vs Post Score by Course Category",
            bar_chart(
                dataset_id,
                "course_category",
                [score_of_kind("baseline", "Avg. Baseline Score"),
                 score_of_kind("post_test", "Avg. Post Score")],
                filter_kinds=("baseline", "post_test"),
                x_axis_label="Course Category",
                y_axis_label="Score (%)",
            ),
            6,
        ),
        ChartSpec(
            "Artefacts and Observations by School",
            bar_chart(
                dataset_id,
                "school_name",
                [count_of_kind("artefact", "Artefacts Submitted"),
                 count_of_kind("classroom_observation", "Classroom Observations")],
                number_format=NUMBER_0,
                x_axis_label="School Name",
                y_axis_label="Activity Count",
            ),
            6,
        ),
        ChartSpec("Participant Scorecard", scorecard_table(dataset_id), 12),
        ChartSpec(
            "Activity Detail",
            raw_table(dataset_id, ACTIVITY_COLUMNS, page_length=50),
            12,
        ),
    ]


def dashboard_specs(dataset_id: int) -> list[DashboardSpec]:
    """Return the three report pages with their filters."""
    return [
        DashboardSpec(
            title="Teacher Report",
            slug=DASHBOARD_SLUGS[0],
            charts=teacher_report(dataset_id),
            filters=[
                ("Participant Name", "participant_name", None),
                ("School Name", "school_name", None),
                ("Course", "course_shortname", None),
                ("Course Category", "course_category", None),
            ],
        ),
        DashboardSpec(
            title="Headmaster Report",
            slug=DASHBOARD_SLUGS[1],
            charts=headmaster_report(dataset_id),
            filters=[
                ("School Name", "school_name", None),
                ("Designation", "designation", None),
                ("Course Category", "course_category", None),
                ("Activity", "activity_kind", None),
            ],
        ),
        DashboardSpec(
            title="Education Officer Report",
            slug=DASHBOARD_SLUGS[2],
            charts=education_officer_report(dataset_id),
            filters=[
                ("District ID", "district_id", None),
                ("School Name", "school_name", None),
                ("Course Category", "course_category", None),
                ("Course", "course_shortname", None),
            ],
        ),
    ]


def select_filter(
    index: int, name: str, column: str, dataset_id: int
) -> dict[str, Any]:
    """Return a dashboard-wide multi-select filter."""
    return {
        "cascadeParentIds": [],
        "controlValues": {
            "defaultToFirstItem": False,
            "enableEmptyFilter": False,
            "inverseSelection": False,
            "multiSelect": True,
            "searchAllOptions": True,
        },
        "defaultDataMask": {"extraFormData": {}, "filterState": {"value": None}},
        "description": "",
        "filterType": "filter_select",
        "id": f"NATIVE_FILTER-{index}",
        "name": name,
        "scope": {"excluded": [], "rootPath": ["ROOT_ID"]},
        "targets": [{"column": {"name": column}, "datasetId": dataset_id}],
    }


def native_filters(
    dataset_id: int, filters: list[tuple[str, str, str | None]]
) -> list[dict[str, Any]]:
    """Return the native filter configuration for one dashboard page."""
    return [
        select_filter(index, name, column, dataset_id)
        for index, (name, column, _default) in enumerate(filters, start=1)
    ]


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


def layout_for(title: str, charts: list[dict[str, Any]]) -> dict[str, Any]:
    """Lay out KPI tiles four per row and the remaining charts two per row."""
    layout: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "HEADER_ID": {
            "id": "HEADER_ID",
            "meta": {"text": title},
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
                    if chart["width"] <= 3
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


def cleanup(api: SupersetApi) -> None:
    """Remove only the artifacts this script owns."""
    for dashboard in api.list_all("dashboard"):
        if dashboard.get("slug") in DASHBOARD_SLUGS:
            api.delete(f"/api/v1/dashboard/{dashboard['id']}")
            print(f"Removed dashboard {dashboard['slug']}")
    for chart in api.list_all("chart"):
        if MARKER in (chart.get("description") or ""):
            api.delete(f"/api/v1/chart/{chart['id']}")
            print(f"Removed chart {chart.get('slice_name')}")
    for dataset in api.list_all("dataset"):
        if dataset.get("table_name") == DATASET_NAME:
            api.delete(f"/api/v1/dataset/{dataset['id']}")
            print(f"Removed dataset {dataset['table_name']}")


def create_dataset(api: SupersetApi, connection: Connection) -> int:
    """Create the read-only virtual dataset over the live Moodle tables."""
    sql = "\n".join(line.rstrip() for line in DATASET_SQL.strip().splitlines())
    result = api.post(
        "/api/v1/dataset/",
        json={
            "database": connection.id,
            "schema": connection.schema,
            "sql": sql,
            "table_name": DATASET_NAME,
        },
    )
    dataset_id = int(result["id"])
    detail = api.get(f"/api/v1/dataset/{dataset_id}")
    columns = detail["result"].get("columns") or []
    if not columns:
        api.put(f"/api/v1/dataset/{dataset_id}/refresh", {})
        detail = api.get(f"/api/v1/dataset/{dataset_id}")
        columns = detail["result"].get("columns") or []
    print(f"Created dataset {DATASET_NAME} (id={dataset_id}, {len(columns)} columns)")
    return dataset_id


def create_chart(
    api: SupersetApi, dashboard_id: int, dataset_id: int, spec: ChartSpec
) -> dict[str, Any]:
    """Create one chart and attach it to the target dashboard."""
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
    return {
        "id": int(response["id"]),
        "name": spec.name,
        "viz_type": spec.viz_type,
        "width": spec.width,
    }


def create_dashboard(api: SupersetApi, spec: DashboardSpec, dataset_id: int) -> int:
    """Create one report page with its charts, layout and filters."""
    result = api.post(
        "/api/v1/dashboard/",
        json={
            "dashboard_title": spec.title,
            "json_metadata": json.dumps({"timed_refresh_immune_slices": []}),
            "position_json": json.dumps({}),
            "published": True,
            "slug": spec.slug,
        },
    )
    dashboard_id = int(result["id"])
    charts = [
        create_chart(api, dashboard_id, dataset_id, chart) for chart in spec.charts
    ]
    print(f"Created {len(charts)} charts on {spec.title}")
    metadata = {
        "color_scheme": "supersetColors",
        "cross_filters_enabled": True,
        "default_filters": "{}",
        "expanded_slices": {},
        "native_filter_configuration": native_filters(dataset_id, spec.filters),
        "refresh_frequency": 0,
        "timed_refresh_immune_slices": [],
    }
    api.put(
        f"/api/v1/dashboard/{dashboard_id}",
        {
            "dashboard_title": spec.title,
            "json_metadata": json.dumps(metadata),
            "position_json": json.dumps(layout_for(spec.title, charts)),
            "published": True,
            "slug": spec.slug,
        },
    )
    return dashboard_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--base-url", help="Superset base URL (default: the .env port)")
    parser.add_argument("--username", help="Superset admin username")
    parser.add_argument("--password", help="Superset admin password")
    parser.add_argument(
        "--connection",
        default=DEFAULT_CONNECTION,
        help="Superset connection holding the Moodle tables",
    )
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
    connection = resolve_connection(api, args.connection)
    cleanup(api)
    dataset_id = create_dataset(api, connection)
    urls = []
    for spec in dashboard_specs(dataset_id):
        create_dashboard(api, spec, dataset_id)
        urls.append(f"{base_url}/superset/dashboard/{spec.slug}/")
    print(
        json.dumps(
            {
                "connection": args.connection,
                "dataset": DATASET_NAME,
                "dashboards": urls,
                "schema": connection.schema,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
