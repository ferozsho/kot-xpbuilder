#!/usr/bin/env python3
"""Generate deterministic, relationship-safe education demo CSV files.

The generated records are synthetic and every file carries a ``data_origin``
column so they cannot be mistaken for production learner data.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any


OUTPUT_DIR = Path(__file__).resolve().parent / "data"
DATA_ORIGIN = "synthetic_demo"

TEACHER_NAMES = (
    "Amina Rahman",
    "Daniel Okafor",
    "Elena Petrova",
    "Hassan Malik",
    "Isabella Costa",
    "Jonas Berg",
    "Mei Chen",
    "Sofia Alvarez",
)

COURSES = (
    ("CRS-001", "Data Literacy Essentials", "Data & Analytics", "TCH-001"),
    ("CRS-002", "Leadership Foundations", "Leadership", "TCH-002"),
    ("CRS-003", "Cybersecurity Awareness", "Compliance", "TCH-003"),
    ("CRS-004", "Project Delivery", "Operations", "TCH-004"),
    ("CRS-005", "Customer Experience", "Customer Success", "TCH-005"),
    ("CRS-006", "Financial Acumen", "Business Skills", "TCH-006"),
    ("CRS-007", "Inclusive Collaboration", "People & Culture", "TCH-007"),
    ("CRS-008", "Applied Generative AI", "Technology", "TCH-008"),
    ("CRS-009", "Strategic Communication", "Business Skills", "TCH-002"),
    ("CRS-010", "Sustainable Operations", "Operations", "TCH-004"),
)

GIVEN_NAMES = (
    "Amara",
    "Ben",
    "Chloe",
    "Darius",
    "Eva",
    "Farah",
    "Gabriel",
    "Hana",
    "Isaac",
    "Julia",
    "Kai",
    "Leila",
    "Mateo",
    "Nadia",
    "Owen",
)

FAMILY_NAMES = ("Adams", "Bello", "Chen", "Diaz")


def write_csv(name: str, rows: list[dict[str, Any]]) -> None:
    """Write non-empty rows using the first row as the canonical schema."""
    if not rows:
        raise ValueError(f"Refusing to generate empty CSV: {name}")
    path = OUTPUT_DIR / name
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def build_rows() -> dict[str, list[dict[str, Any]]]:
    """Build normalized demo rows and their summary tables."""
    start = date(2025, 9, 1)
    students: list[dict[str, Any]] = []
    enrollments: list[dict[str, Any]] = []
    progress_rows: list[dict[str, Any]] = []
    assessments: list[dict[str, Any]] = []

    for student_index in range(60):
        student_id = f"STU-{student_index + 1:03d}"
        student_name = (
            f"{GIVEN_NAMES[student_index % len(GIVEN_NAMES)]} "
            f"{FAMILY_NAMES[student_index // len(GIVEN_NAMES)]}"
        )
        student_enrollment_date = start + timedelta(days=(student_index * 5) % 330)
        enrolled_course_ids = []
        progress_values = []
        score_values = []
        completion_values = []
        last_activity_values = []

        for course_offset in range(2):
            course_index = (student_index * 3 + course_offset * 4) % len(COURSES)
            course_id = COURSES[course_index][0]
            enrolled_course_ids.append(course_id)
            enrollment_id = f"ENR-{student_index * 2 + course_offset + 1:03d}"
            enrollment_date = student_enrollment_date + timedelta(
                days=course_offset * 12
            )
            progress = (
                100
                if student_index % 5 == 0
                else min(99, 28 + ((student_index * 17 + course_offset * 29) % 72))
            )
            status = "Completed" if progress == 100 else "In Progress"
            last_activity = min(
                date(2026, 8, 31),
                enrollment_date + timedelta(days=18 + progress),
            )
            completion_date = (
                (enrollment_date + timedelta(days=progress)).isoformat()
                if status == "Completed"
                else ""
            )

            enrollments.append(
                {
                    "enrollment_id": enrollment_id,
                    "student_id": student_id,
                    "course_id": course_id,
                    "enrollment_date": enrollment_date.isoformat(),
                    "enrollment_status": status,
                    "completion_date": completion_date,
                    "data_origin": DATA_ORIGIN,
                }
            )
            progress_rows.append(
                {
                    "progress_id": f"PRG-{student_index * 2 + course_offset + 1:03d}",
                    "enrollment_id": enrollment_id,
                    "student_id": student_id,
                    "course_id": course_id,
                    "progress_percent": progress,
                    "completion_status": status,
                    "last_activity_date": last_activity.isoformat(),
                    "data_origin": DATA_ORIGIN,
                }
            )

            enrollment_scores = []
            for assessment_number in range(1, 3):
                score = min(
                    100,
                    48
                    + (student_index * 7 + course_index * 5 + assessment_number * 11)
                    % 53,
                )
                assessment_date = min(
                    date(2026, 8, 31),
                    enrollment_date + timedelta(days=assessment_number * 21),
                )
                assessments.append(
                    {
                        "assessment_result_id": (
                            f"ASM-{(student_index * 4) + (course_offset * 2) + assessment_number:03d}"
                        ),
                        "enrollment_id": enrollment_id,
                        "student_id": student_id,
                        "course_id": course_id,
                        "assessment_name": f"Knowledge Check {assessment_number}",
                        "score": score,
                        "max_score": 100,
                        "assessment_date": assessment_date.isoformat(),
                        "result_status": "Passed" if score >= 60 else "Needs Support",
                        "data_origin": DATA_ORIGIN,
                    }
                )
                enrollment_scores.append(score)

            progress_values.append(progress)
            score_values.extend(enrollment_scores)
            completion_values.append(status)
            last_activity_values.append(last_activity)

        students.append(
            {
                "student_id": student_id,
                "student_name": student_name,
                "enrollment_date": student_enrollment_date.isoformat(),
                "enrolled_courses": "; ".join(enrolled_course_ids),
                "course_progress": round(
                    sum(progress_values) / len(progress_values), 1
                ),
                "completion_status": (
                    "Completed"
                    if all(value == "Completed" for value in completion_values)
                    else "Active"
                ),
                "assessment_score": round(sum(score_values) / len(score_values), 1),
                "last_activity_date": max(last_activity_values).isoformat(),
                "data_origin": DATA_ORIGIN,
            }
        )

    enrollment_by_course: dict[str, list[dict[str, Any]]] = defaultdict(list)
    progress_by_course: dict[str, list[dict[str, Any]]] = defaultdict(list)
    scores_by_course: dict[str, list[int]] = defaultdict(list)
    for enrollment in enrollments:
        enrollment_by_course[enrollment["course_id"]].append(enrollment)
    for progress in progress_rows:
        progress_by_course[progress["course_id"]].append(progress)
    for assessment in assessments:
        scores_by_course[assessment["course_id"]].append(assessment["score"])

    courses = []
    for course_id, course_name, category, teacher_id in COURSES:
        course_enrollments = enrollment_by_course[course_id]
        course_progress = progress_by_course[course_id]
        completed = sum(
            row["completion_status"] == "Completed" for row in course_progress
        )
        enrollment_count = len(course_enrollments)
        courses.append(
            {
                "course_id": course_id,
                "course_name": course_name,
                "category": category,
                "assigned_teacher_id": teacher_id,
                "enrollment_count": enrollment_count,
                "completion_count": completed,
                "completion_percentage": round(100 * completed / enrollment_count, 1),
                "average_grade": round(
                    sum(scores_by_course[course_id]) / len(scores_by_course[course_id]),
                    1,
                ),
                "course_status": "Active",
                "data_origin": DATA_ORIGIN,
            }
        )

    teachers = []
    for teacher_index, teacher_name in enumerate(TEACHER_NAMES, 1):
        teacher_id = f"TCH-{teacher_index:03d}"
        assigned_course_ids = [
            course_id for course_id, _, _, owner_id in COURSES if owner_id == teacher_id
        ]
        teacher_enrollments = [
            row for row in enrollments if row["course_id"] in assigned_course_ids
        ]
        teacher_progress = [
            row for row in progress_rows if row["course_id"] in assigned_course_ids
        ]
        teacher_scores = [
            row["score"]
            for row in assessments
            if row["course_id"] in assigned_course_ids
        ]
        completed = sum(
            row["completion_status"] == "Completed" for row in teacher_progress
        )
        teachers.append(
            {
                "teacher_id": teacher_id,
                "teacher_name": teacher_name,
                "assigned_courses": "; ".join(assigned_course_ids),
                "assigned_course_count": len(assigned_course_ids),
                "total_students": len(
                    {row["student_id"] for row in teacher_enrollments}
                ),
                "student_completion_count": completed,
                "student_completion_percentage": round(
                    100 * completed / len(teacher_progress), 1
                ),
                "average_course_performance": round(
                    sum(teacher_scores) / len(teacher_scores), 1
                ),
                "data_origin": DATA_ORIGIN,
            }
        )

    return {
        "students.csv": students,
        "teachers.csv": teachers,
        "courses.csv": courses,
        "enrollments.csv": enrollments,
        "course_progress.csv": progress_rows,
        "assessment_results.csv": assessments,
    }


def validate(rows_by_file: dict[str, list[dict[str, Any]]]) -> None:
    """Validate required values, unique keys, and every foreign key."""
    for filename, rows in rows_by_file.items():
        if not rows or any(not row for row in rows):
            raise ValueError(f"{filename} contains an unexpected empty record")
        if any(row.get("data_origin") != DATA_ORIGIN for row in rows):
            raise ValueError(f"{filename} is missing the synthetic data label")

    students = {row["student_id"] for row in rows_by_file["students.csv"]}
    teachers = {row["teacher_id"] for row in rows_by_file["teachers.csv"]}
    courses = {row["course_id"] for row in rows_by_file["courses.csv"]}
    enrollments = {row["enrollment_id"] for row in rows_by_file["enrollments.csv"]}
    if len(students) != len(rows_by_file["students.csv"]):
        raise ValueError("Duplicate student IDs")
    if len(enrollments) != len(rows_by_file["enrollments.csv"]):
        raise ValueError("Duplicate enrollment IDs")
    if any(
        row["assigned_teacher_id"] not in teachers
        for row in rows_by_file["courses.csv"]
    ):
        raise ValueError("Unknown teacher in courses.csv")
    for filename in (
        "enrollments.csv",
        "course_progress.csv",
        "assessment_results.csv",
    ):
        if any(row["student_id"] not in students for row in rows_by_file[filename]):
            raise ValueError(f"Unknown student in {filename}")
        if any(row["course_id"] not in courses for row in rows_by_file[filename]):
            raise ValueError(f"Unknown course in {filename}")
    for filename in ("course_progress.csv", "assessment_results.csv"):
        if any(
            row["enrollment_id"] not in enrollments for row in rows_by_file[filename]
        ):
            raise ValueError(f"Unknown enrollment in {filename}")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows_by_file = build_rows()
    validate(rows_by_file)
    for filename, rows in rows_by_file.items():
        write_csv(filename, rows)
        print(f"{filename}: {len(rows)} records")


if __name__ == "__main__":
    main()
