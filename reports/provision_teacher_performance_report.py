#!/usr/bin/env python3
"""Provision the Teacher Performance Report dashboard on a live Moodle database.

Rebuilds the client's *Teacher Performance* report (teacher selector, grade
selector, KPI row, BL/ML chart, concept-wise micro-assessment chart and the
per-student gain chart) as a Superset dashboard whose datasets are read-only
virtual datasets over the connected Moodle database. Nothing is written to the
connected database: no table is created, altered or dropped.

The reference report reads a flat export that does not exist in the Moodle
database, so each element is derived from the closest live equivalent:

=========================  ===================================================
Report element             Live source
=========================  ===================================================
Teacher                    ``mdl_local_classroom_trainers.trainerid`` -> user
Grade                      ``mdl_local_classroom.open_standard`` ->
                           ``mdl_local_standard`` ("Grade 5")
Class / section            ``mdl_local_classroom.name``
School                     ``mdl_local_classroom.open_school`` ->
                           ``mdl_local_school``
Student                    ``mdl_local_classroom_users.userid`` -> user
Courses assigned           distinct courses the teacher is enrolled in
Courses completed          ``mdl_course_completions`` for those courses
Artefacts submitted        the teacher's submitted ``mdl_assign_submission``
                           rows
Baseline (BL) score        the class students' graded *baseline* / *pre* items,
                           as a percentage of each item's maximum
Mastery (ML) score         the class students' graded *midline* / *endline* /
                           *post* items, as a percentage of the maximum
Student gain %             (ML - BL) / BL, per student
Micro-assessment concept   graded items of the *Micro Assessment* courses,
                           grouped by the assessment (concept) name
=========================  ===================================================

Known data gaps in the client database (see ``reports/README.md``): the
micro-assessment activities exist but carry no grades yet, and the live items
have no common 20-point scale, so BL/ML are normalised to the percentage of
each assessment's maximum instead of being reported "out of 20".

```bash
python3 reports/provision_teacher_performance_report.py \
    --base-url https://kot5.example.com \
    --username admin --password '<admin password>' \
    --connection kefuat
```

Without ``--base-url`` the script reads ``XPBUILDER_HOST_PORT``,
``SUPERSET_ADMIN_USERNAME`` and ``SUPERSET_ADMIN_PASSWORD`` from ``--env-file``
(default ``./.env``). The script is idempotent: only the datasets, charts and
dashboard marked with ``SLUG``/``MARKER`` are replaced.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

MARKER = "XPBuilder teacher performance report"
SLUG = "teacher-performance-report"
TITLE = "Teacher Performance Report"
DEFAULT_CONNECTION = "kefuat"

STUDENT_DATASET = "Teacher Student Performance"
COURSE_DATASET = "Teacher Course Report"
CONCEPT_DATASET = "Teacher Concept Assessment"

DATASET_NAMES = (STUDENT_DATASET, COURSE_DATASET, CONCEPT_DATASET)

# One row per (teacher x class x student). Every teacher-level figure (courses,
# artefacts) is carried on the student rows so a single dataset feeds the whole
# KPI row, and every metric uses COUNT(DISTINCT ...) or an average over the
# student grain so joined tables cannot inflate the numbers.
STUDENT_SQL = """
WITH teacher_classes AS (
    SELECT DISTINCT t.trainerid AS teacher_id, t.classroomid AS class_id
    FROM mdl_local_classroom_trainers t
),
class_students AS (
    SELECT DISTINCT tc.teacher_id, tc.class_id, cu.userid AS student_id
    FROM teacher_classes tc
    JOIN mdl_local_classroom_users cu ON cu.classroomid = tc.class_id
),
student_baseline AS (
    SELECT gg.userid AS student_id,
           AVG(gg.finalgrade / gi.grademax * 100) AS baseline_pct,
           MAX(DATE(FROM_UNIXTIME(gg.timemodified))) AS baseline_on
    FROM mdl_grade_grades gg
    JOIN mdl_grade_items gi ON gi.id = gg.itemid
    WHERE gg.finalgrade IS NOT NULL
      AND gi.grademax > 0
      AND gi.itemname IS NOT NULL
      AND (LOWER(gi.itemname) LIKE '%baseline%'
        OR LOWER(gi.itemname) LIKE '%pre test%'
        OR LOWER(gi.itemname) LIKE '%pre-test%'
        OR LOWER(gi.itemname) LIKE '%pretest%'
        OR LOWER(gi.itemname) LIKE '%pre course%'
        OR LOWER(gi.itemname) LIKE '%pre-course%'
        OR LOWER(gi.itemname) LIKE '%pre session%'
        OR LOWER(gi.itemname) LIKE '%pre-survey%'
        OR LOWER(gi.itemname) LIKE '%pre survey%')
    GROUP BY gg.userid
),
student_mastery AS (
    SELECT gg.userid AS student_id,
           AVG(gg.finalgrade / gi.grademax * 100) AS mastery_pct,
           MAX(DATE(FROM_UNIXTIME(gg.timemodified))) AS mastery_on
    FROM mdl_grade_grades gg
    JOIN mdl_grade_items gi ON gi.id = gg.itemid
    WHERE gg.finalgrade IS NOT NULL
      AND gi.grademax > 0
      AND gi.itemname IS NOT NULL
      AND (LOWER(gi.itemname) LIKE '%midline%'
        OR LOWER(gi.itemname) LIKE '%mid line%'
        OR LOWER(gi.itemname) LIKE '%mid-line%'
        OR LOWER(gi.itemname) LIKE '%endline%'
        OR LOWER(gi.itemname) LIKE '%post test%'
        OR LOWER(gi.itemname) LIKE '%post-test%'
        OR LOWER(gi.itemname) LIKE '%postest%'
        OR LOWER(gi.itemname) LIKE '%post course%'
        OR LOWER(gi.itemname) LIKE '%post session%'
        OR LOWER(gi.itemname) LIKE '%post survey%')
    GROUP BY gg.userid
),
student_volume AS (
    SELECT gg.userid AS student_id, COUNT(*) AS grades_recorded
    FROM mdl_grade_grades gg
    WHERE gg.finalgrade IS NOT NULL
    GROUP BY gg.userid
),
teacher_course_rows AS (
    SELECT tr.trainerid AS teacher_id, e.courseid AS course_id,
           0 AS completed_flag
    FROM (SELECT DISTINCT trainerid FROM mdl_local_classroom_trainers) tr
    JOIN mdl_user_enrolments ue ON ue.userid = tr.trainerid
    JOIN mdl_enrol e ON e.id = ue.enrolid
    UNION
    SELECT cc.userid AS teacher_id, cc.course AS course_id,
           1 AS completed_flag
    FROM mdl_course_completions cc
    WHERE cc.userid IN (SELECT DISTINCT trainerid FROM mdl_local_classroom_trainers)
),
teacher_courses AS (
    SELECT r.teacher_id,
           COUNT(DISTINCT r.course_id) AS courses_assigned,
           COUNT(DISTINCT CASE WHEN r.completed_flag = 1 THEN r.course_id END)
               AS courses_completed
    FROM teacher_course_rows r
    GROUP BY r.teacher_id
),
teacher_artefacts AS (
    SELECT s.userid AS teacher_id, COUNT(*) AS artefacts_submitted
    FROM mdl_assign_submission s
    WHERE s.status = 'submitted'
      AND s.userid IN (SELECT trainerid FROM mdl_local_classroom_trainers)
    GROUP BY s.userid
),
graded AS (
    SELECT cs.teacher_id, cs.class_id, cs.student_id,
           cls.name AS class_name,
           NULLIF(cls.open_standard, '') AS grade_code,
           CASE WHEN cls.open_standard REGEXP '^[0-9]+$'
                THEN CAST(cls.open_standard AS UNSIGNED) END AS grade_id,
           NULLIF(cls.open_school, '') AS school_code,
           tc.courses_assigned, tc.courses_completed,
           COALESCE(ta.artefacts_submitted, 0) AS artefacts_submitted,
           sb.baseline_pct, sm.mastery_pct,
           COALESCE(sv.grades_recorded, 0) AS grades_recorded,
           GREATEST(COALESCE(sb.baseline_on, '1900-01-01'),
                    COALESCE(sm.mastery_on, '1900-01-01')) AS assessed_on
    FROM class_students cs
    JOIN mdl_local_classroom cls ON cls.id = cs.class_id
    LEFT JOIN student_baseline sb ON sb.student_id = cs.student_id
    LEFT JOIN student_mastery sm ON sm.student_id = cs.student_id
    LEFT JOIN student_volume sv ON sv.student_id = cs.student_id
    LEFT JOIN teacher_courses tc ON tc.teacher_id = cs.teacher_id
    LEFT JOIN teacher_artefacts ta ON ta.teacher_id = cs.teacher_id
)
SELECT
    g.teacher_id,
    CONCAT(tu.firstname, ' ', tu.lastname) AS teacher_name,
    tu.username AS teacher_username,
    g.grade_id,
    COALESCE(CONCAT('Grade ', g.grade_code), 'Grade not set') AS grade_name,
    g.class_id,
    g.class_name,
    sc.id AS school_id,
    COALESCE(sc.school_name, 'No school set') AS school_name,
    g.student_id,
    CONCAT(su.firstname, ' ', su.lastname) AS student_name,
    su.username AS student_username,
    COALESCE(g.courses_assigned, 0) AS courses_assigned,
    COALESCE(g.courses_completed, 0) AS courses_completed,
    g.artefacts_submitted,
    ROUND(g.baseline_pct, 2) AS baseline_score,
    ROUND(g.mastery_pct, 2) AS mastery_score,
    CASE
        WHEN g.baseline_pct IS NULL OR g.mastery_pct IS NULL
          OR g.baseline_pct = 0 THEN NULL
        ELSE ROUND((g.mastery_pct - g.baseline_pct) / g.baseline_pct, 4)
    END AS student_gain,
    g.grades_recorded AS assessments_recorded,
    NULLIF(g.assessed_on, '1900-01-01') AS last_assessment_date
FROM graded g
JOIN mdl_user tu ON tu.id = g.teacher_id
JOIN mdl_user su ON su.id = g.student_id
LEFT JOIN mdl_local_school sc
    ON g.school_code REGEXP '^[0-9]+$'
   AND sc.id = CAST(g.school_code AS UNSIGNED)
"""

# One row per (teacher x course) for the teacher's own course enrolments.
COURSE_SQL = """
WITH trainers AS (
    SELECT DISTINCT t.trainerid AS teacher_id FROM mdl_local_classroom_trainers t
),
course_rows AS (
    SELECT tr.teacher_id, e.courseid AS course_id, 0 AS completed_flag
    FROM trainers tr
    JOIN mdl_user_enrolments ue ON ue.userid = tr.teacher_id
    JOIN mdl_enrol e ON e.id = ue.enrolid
    UNION
    SELECT cc.userid AS teacher_id, cc.course AS course_id,
           1 AS completed_flag
    FROM mdl_course_completions cc
    WHERE cc.userid IN (SELECT teacher_id FROM trainers)
),
assigned AS (
    SELECT r.teacher_id, r.course_id, MAX(r.completed_flag) AS completed_flag
    FROM course_rows r
    GROUP BY r.teacher_id, r.course_id
),
artefacts AS (
    SELECT s.userid AS teacher_id, COUNT(*) AS artefacts_submitted
    FROM mdl_assign_submission s
    WHERE s.status = 'submitted'
      AND s.userid IN (SELECT teacher_id FROM trainers)
    GROUP BY s.userid
)
SELECT
    a.teacher_id,
    CONCAT(u.firstname, ' ', u.lastname) AS teacher_name,
    a.course_id,
    c.fullname AS course_name,
    c.shortname AS course_shortname,
    COALESCE(cc2.name, 'Uncategorised') AS course_category,
    a.completed_flag,
    COALESCE(ar.artefacts_submitted, 0) AS artefacts_submitted
FROM assigned a
JOIN mdl_user u ON u.id = a.teacher_id
JOIN mdl_course c ON c.id = a.course_id
LEFT JOIN mdl_course_categories cc2 ON cc2.id = c.category
LEFT JOIN artefacts ar ON ar.teacher_id = a.teacher_id
"""

# One row per (teacher x class x student x concept) for the micro-assessment /
# concept-wise chart. The score is the student's average for that concept, so
# the chart's average weights every student equally.
CONCEPT_SQL = """
WITH teacher_classes AS (
    SELECT DISTINCT t.trainerid AS teacher_id, t.classroomid AS class_id
    FROM mdl_local_classroom_trainers t
),
class_students AS (
    SELECT DISTINCT tc.teacher_id, tc.class_id, cu.userid AS student_id
    FROM teacher_classes tc
    JOIN mdl_local_classroom_users cu ON cu.classroomid = tc.class_id
),
micro_courses AS (
    SELECT c.id AS course_id
    FROM mdl_course c
    WHERE LOWER(c.fullname) LIKE '%micro assessment%'
       OR LOWER(c.fullname) LIKE '%micro-assessment%'
),
concept_scores AS (
    SELECT cs.teacher_id, cs.class_id, cs.student_id,
           gi.itemname AS concept_name,
           AVG(gg.finalgrade / gi.grademax * 100) AS concept_score,
           COUNT(*) AS assessments_recorded,
           MAX(DATE(FROM_UNIXTIME(gg.timemodified))) AS concept_on
    FROM class_students cs
    JOIN mdl_grade_grades gg ON gg.userid = cs.student_id
    JOIN mdl_grade_items gi ON gi.id = gg.itemid
    JOIN micro_courses mc ON mc.course_id = gi.courseid
    WHERE gg.finalgrade IS NOT NULL
      AND gi.grademax > 0
      AND gi.itemname IS NOT NULL
    GROUP BY cs.teacher_id, cs.class_id, cs.student_id, gi.itemname
)
SELECT
    t.teacher_id,
    CONCAT(tu.firstname, ' ', tu.lastname) AS teacher_name,
    NULLIF(cls.open_standard, '') AS grade_code,
    CASE WHEN cls.open_standard REGEXP '^[0-9]+$'
         THEN CAST(cls.open_standard AS UNSIGNED) END AS grade_id,
    COALESCE(CONCAT('Grade ', NULLIF(cls.open_standard, '')), 'Grade not set')
        AS grade_name,
    t.class_id,
    cls.name AS class_name,
    t.student_id,
    CONCAT(su.firstname, ' ', su.lastname) AS student_name,
    t.concept_name,
    ROUND(t.concept_score, 2) AS microassessment_score,
    t.assessments_recorded,
    t.concept_on AS last_assessment_date
FROM concept_scores t
JOIN mdl_local_classroom cls ON cls.id = t.class_id
JOIN mdl_user tu ON tu.id = t.teacher_id
JOIN mdl_user su ON su.id = t.student_id
"""

PERCENT_1 = ".1%"
NUMBER_1 = ",.1f"
NUMBER_0 = ",.0f"


@dataclass
class ChartSpec:
    """A chart to create, with its width in the 12-column dashboard grid."""

    key: str
    name: str
    dataset_key: str
    form: dict[str, Any]
    width: int
    height: int
    viz_type: str = field(init=False)

    def __post_init__(self) -> None:
        self.viz_type = self.form["viz_type"]


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
            "show_metric_name": False,
            "subheader": label,
            "subheader_font_size": 0.16,
            "y_axis_format": number_format,
        }
    )
    return form


def not_null_filter(column: str) -> dict[str, Any]:
    """Return an adhoc filter that keeps the rows where a column is set."""
    return {
        "clause": "WHERE",
        "expressionType": "SIMPLE",
        "operator": "IS NOT NULL",
        "subject": column,
    }


def bar_chart(
    dataset_id: int,
    x_axis: str,
    metrics: list[dict[str, Any]],
    *,
    number_format: str = NUMBER_1,
    orientation: str = "vertical",
    sort_by_value: bool = False,
    x_axis_label: str | None = None,
    y_axis_label: str | None = None,
    row_limit: int = 10000,
) -> dict[str, Any]:
    """Return a clustered bar chart with value labels."""
    form = base_form(dataset_id, "echarts_timeseries_bar")
    form.update(
        {
            "metrics": metrics,
            "orientation": orientation,
            "rich_tooltip": True,
            "row_limit": row_limit,
            "show_value": True,
            "x_axis": x_axis,
            "x_axis_label_rotation": 25 if orientation == "vertical" else 0,
            "x_axis_title": x_axis_label or x_axis.replace("_", " ").title(),
            "y_axis_format": number_format,
            "y_axis_title": y_axis_label or "",
        }
    )
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
    x_axis_label: str | None = None,
    y_axis_label: str | None = None,
    filters: tuple[dict[str, Any], ...] = (),
) -> dict[str, Any]:
    """Return a filled area chart with point markers and value labels."""
    form = base_form(dataset_id, "echarts_area")
    form.update(
        {
            "metrics": [metric_definition],
            "opacity": 0.35,
            "rich_tooltip": True,
            "show_value": True,
            "x_axis": x_axis,
            "x_axis_label_rotation": 20,
            "x_axis_title": x_axis_label or x_axis.replace("_", " ").title(),
            "y_axis_format": number_format,
            "y_axis_title": y_axis_label or "",
        }
    )
    if filters:
        form["adhoc_filters"] = list(filters)
    return form


def charts(datasets: dict[str, int]) -> list[ChartSpec]:
    """Return the report tiles, grouped by the dataset they read."""
    student = datasets["student"]
    course = datasets["course"]
    concept = datasets["concept"]

    courses_assigned = "COUNT(DISTINCT course_id)"
    courses_completed = (
        "COUNT(DISTINCT CASE WHEN completed_flag = 1 THEN course_id END)"
    )
    completion_pct = f"{courses_completed} / NULLIF({courses_assigned}, 0)"
    gain = "AVG(student_gain)"

    return [
        ChartSpec(
            "kpi_courses",
            "Courses Enrolled Vs Completed",
            "course",
            kpi(
                course,
                sql_metric(completion_pct, "Courses Enrolled Vs Completed"),
                "Courses Completed %",
                PERCENT_1,
            ),
            3,
            26,
        ),
        ChartSpec(
            "kpi_students",
            "Total Students",
            "student",
            kpi(
                student,
                simple("student_id", "COUNT_DISTINCT", "Total Students"),
                "Total Students",
            ),
            3,
            26,
        ),
        ChartSpec(
            "kpi_gain",
            "Student Gain %",
            "student",
            kpi(student, sql_metric(gain, "Student Gain %"),
                "Student Gain %", PERCENT_1),
            3,
            26,
        ),
        ChartSpec(
            "kpi_artefacts",
            "Artefacts Submitted",
            "student",
            kpi(
                student,
                sql_metric("MAX(artefacts_submitted)", "Artefacts Submitted"),
                "Artefacts Submitted",
            ),
            3,
            26,
        ),
        ChartSpec(
            "bl_ml",
            "Avg. BL Score and Avg. ML Score",
            "student",
            bar_chart(
                student,
                "grade_name",
                [
                    sql_metric("AVG(baseline_score)", "Avg. BL Score"),
                    sql_metric("AVG(mastery_score)", "Avg. ML Score"),
                ],
                number_format=NUMBER_1,
                x_axis_label="Grade",
                y_axis_label="",
            ),
            5,
            44,
        ),
        ChartSpec(
            "concept",
            "Microassessment Score Concept Wise",
            "concept",
            bar_chart(
                concept,
                "concept_name",
                [sql_metric("AVG(microassessment_score)", "Microassessment Score")],
                number_format=NUMBER_1,
                x_axis_label="Concept",
                y_axis_label="",
            ),
            7,
            44,
        ),
        ChartSpec(
            "course_bar",
            "Courses Enrolled vs Courses Completed",
            "course",
            bar_chart(
                course,
                "teacher_name",
                [
                    sql_metric(courses_assigned, "Courses Assigned"),
                    sql_metric(courses_completed, "Courses Completed"),
                ],
                number_format=NUMBER_0,
                orientation="horizontal",
                sort_by_value=True,
                x_axis_label="Teacher",
                y_axis_label="Courses",
                row_limit=12,
            ),
            6,
            44,
        ),
        ChartSpec(
            "gain_area",
            "Student Gain %",
            "student",
            area_chart(
                student,
                "student_name",
                sql_metric(gain, "Student Gain %"),
                x_axis_label="Student Name",
                y_axis_label="Student Gain %",
                filters=(not_null_filter("student_gain"),),
            ),
            6,
            44,
        ),
    ]


def select_filter(
    index: int,
    name: str,
    column: str,
    targets: list[tuple[int, str]],
    *,
    multi: bool = True,
    cascade_from: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return a dashboard-wide select filter for one or more datasets."""
    return {
        "cascadeParentIds": list(cascade_from),
        "controlValues": {
            "defaultToFirstItem": False,
            "enableEmptyFilter": False,
            "inverseSelection": False,
            "multiSelect": multi,
            "searchAllOptions": True,
        },
        "defaultDataMask": {"extraFormData": {}, "filterState": {"value": None}},
        "description": "",
        "filterType": "filter_select",
        "id": f"NATIVE_FILTER-{index}",
        "name": name,
        "scope": {"excluded": [], "rootPath": ["ROOT_ID"]},
        "targets": [
            {"column": {"name": column}, "datasetId": dataset_id}
            for dataset_id, column in targets
        ],
    }


def time_filter(
    index: int, name: str, targets: list[tuple[int, str]]
) -> dict[str, Any]:
    """Return a dashboard-wide date-range filter."""
    return {
        "cascadeParentIds": [],
        "controlValues": {"enableEmptyFilter": False},
        "defaultDataMask": {"extraFormData": {}, "filterState": {"value": None}},
        "description": "",
        "filterType": "filter_time",
        "id": f"NATIVE_FILTER-{index}",
        "name": name,
        "scope": {"excluded": [], "rootPath": ["ROOT_ID"]},
        "targets": [
            {"column": {"name": column}, "datasetId": dataset_id}
            for dataset_id, column in targets
        ],
    }


def native_filters(datasets: dict[str, int]) -> list[dict[str, Any]]:
    """Return the dashboard filters, shared by the three datasets."""
    student = datasets["student"]
    course = datasets["course"]
    concept = datasets["concept"]
    student_concept = [(student, "teacher_name"), (concept, "teacher_name")]
    return [
        select_filter(
            1,
            "Teacher Name",
            "teacher_name",
            student_concept + [(course, "teacher_name")],
        ),
        select_filter(
            2,
            "Grade",
            "grade_name",
            [(student, "grade_name"), (concept, "grade_name")],
            cascade_from=("NATIVE_FILTER-1",),
        ),
        select_filter(
            3,
            "Class / Section",
            "class_name",
            [(student, "class_name"), (concept, "class_name")],
            cascade_from=("NATIVE_FILTER-1", "NATIVE_FILTER-2"),
        ),
        select_filter(
            4,
            "School",
            "school_name",
            [(student, "school_name")],
            cascade_from=("NATIVE_FILTER-1",),
        ),
        select_filter(
            5,
            "Course",
            "course_name",
            [(course, "course_name")],
            cascade_from=("NATIVE_FILTER-1",),
        ),
        time_filter(
            6,
            "Assessment Date",
            [
                (student, "last_assessment_date"),
                (concept, "last_assessment_date"),
            ],
        ),
    ]


def layout(chart_rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    """Build the dashboard grid, one row per supplied row of charts."""
    result: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "HEADER_ID": {
            "id": "HEADER_ID",
            "meta": {"text": TITLE},
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
    for chart in [c for row in chart_rows for c in row]:
        component_id = f"CHART-{chart['id']}"
        result[component_id] = {
            "children": [],
            "id": component_id,
            "meta": {
                "chartId": chart["id"],
                "height": chart["height"],
                "sliceName": chart["name"],
                "width": chart["width"],
            },
            "parents": ["ROOT_ID", "GRID_ID"],
            "type": "CHART",
        }
    for row_number, row in enumerate(chart_rows):
        row_id = f"ROW-{row_number:02d}"
        result[row_id] = {
            "children": [f"CHART-{chart['id']}" for chart in row],
            "id": row_id,
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
            "parents": ["ROOT_ID", "GRID_ID"],
            "type": "ROW",
        }
        for chart in row:
            result[f"CHART-{chart['id']}"]["parents"].append(row_id)
        result["GRID_ID"]["children"].append(row_id)
    return result


DASHBOARD_CSS = """
/* Reference-report card styling: white rounded cards with a soft shadow. */
.dashboard-content .dashboard-component-chart-holder {
  background: #ffffff !important;
  border: 1px solid #eef0f3 !important;
  border-radius: 12px !important;
  box-shadow: 0 1px 3px rgba(16, 24, 40, 0.10), 0 4px 12px rgba(16, 24, 40, 0.06)
    !important;
}
.dashboard-content .dashboard-component-chart-holder .chart-container {
  background: #ffffff !important;
}
.dashboard-content {
  background: #f7f8fa !important;
}
"""


def cleanup(api: SupersetApi) -> None:
    """Remove only the artifacts this script owns."""
    for dashboard in api.list_all("dashboard"):
        if dashboard.get("slug") == SLUG:
            api.delete(f"/api/v1/dashboard/{dashboard['id']}")
            print(f"Removed dashboard {SLUG}")
    for chart in api.list_all("chart"):
        if MARKER in (chart.get("description") or ""):
            api.delete(f"/api/v1/chart/{chart['id']}")
            print(f"Removed chart {chart.get('slice_name')}")
    for dataset in api.list_all("dataset"):
        if dataset.get("table_name") in DATASET_NAMES:
            api.delete(f"/api/v1/dataset/{dataset['id']}")
            print(f"Removed dataset {dataset['table_name']}")


def create_dataset(
    api: SupersetApi, connection: Connection, table_name: str, sql: str
) -> int:
    """Create one read-only virtual dataset over the live Moodle tables."""
    normalised = "\n".join(line.rstrip() for line in sql.strip().splitlines())
    result = api.post(
        "/api/v1/dataset/",
        json={
            "database": connection.id,
            "schema": connection.schema,
            "sql": normalised,
            "table_name": table_name,
        },
    )
    dataset_id = int(result["id"])
    detail = api.get(f"/api/v1/dataset/{dataset_id}")
    columns = detail["result"].get("columns") or []
    if not columns:
        api.put(f"/api/v1/dataset/{dataset_id}/refresh", {})
        detail = api.get(f"/api/v1/dataset/{dataset_id}")
        columns = detail["result"].get("columns") or []
    print(f"Created dataset {table_name} (id={dataset_id}, {len(columns)} columns)")
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
        "width": spec.width,
        "height": spec.height,
    }


def create_dashboard(
    api: SupersetApi, specs: list[ChartSpec], datasets: dict[str, int]
) -> int:
    """Create the dashboard with its charts, layout, filters and styling."""
    result = api.post(
        "/api/v1/dashboard/",
        json={
            "dashboard_title": TITLE,
            "json_metadata": json.dumps({"timed_refresh_immune_slices": []}),
            "position_json": json.dumps({}),
            "published": True,
            "slug": SLUG,
        },
    )
    dashboard_id = int(result["id"])
    created = {
        spec.key: create_chart(api, dashboard_id, datasets[spec.dataset_key], spec)
        for spec in specs
    }
    rows = [
        [
            created["kpi_courses"],
            created["kpi_students"],
            created["kpi_gain"],
            created["kpi_artefacts"],
        ],
        [created["bl_ml"], created["concept"]],
        [created["course_bar"], created["gain_area"]],
    ]
    metadata = {
        "color_scheme": "supersetColors",
        "cross_filters_enabled": True,
        "default_filters": "{}",
        "expanded_slices": {},
        "filter_bar_orientation": "HORIZONTAL",
        "native_filter_configuration": native_filters(datasets),
        "refresh_frequency": 0,
        "timed_refresh_immune_slices": [],
    }
    api.put(
        f"/api/v1/dashboard/{dashboard_id}",
        {
            "css": DASHBOARD_CSS,
            "dashboard_title": TITLE,
            "json_metadata": json.dumps(metadata),
            "position_json": json.dumps(layout(rows)),
            "published": True,
            "slug": SLUG,
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
    datasets = {
        "student": create_dataset(api, connection, STUDENT_DATASET, STUDENT_SQL),
        "course": create_dataset(api, connection, COURSE_DATASET, COURSE_SQL),
        "concept": create_dataset(api, connection, CONCEPT_DATASET, CONCEPT_SQL),
    }
    specs = charts(datasets)
    dashboard_id = create_dashboard(api, specs, datasets)
    print(f"Created {len(specs)} charts on {TITLE} (id={dashboard_id})")
    print(
        json.dumps(
            {
                "connection": args.connection,
                "datasets": datasets,
                "dashboard": f"{base_url}/superset/dashboard/{SLUG}/",
                "schema": connection.schema,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
