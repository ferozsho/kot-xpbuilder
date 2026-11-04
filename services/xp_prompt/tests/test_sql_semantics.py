"""Tests for semantic validation (P4-F07).

The bias under test is silence: a check must fire only when it is sure, because a
false refusal blocks a legitimate question.
"""

from __future__ import annotations

import unittest

from xp_prompt import schema_slice, sql_semantics


def dataset(**overrides):
    entry = {
        "dataset_id": 59,
        "name": "Student Overview",
        "schema": "public",
        "database": "File uploads",
        "dialect": "postgres",
        "sql": "SELECT 1 AS one",
        "columns": [
            {"name": "student_id", "type": "STRING"},
            {"name": "student_name", "type": "STRING"},
            {"name": "completion_status", "type": "STRING"},
            {"name": "assessment_score", "type": "INTEGER"},
            {"name": "progress", "type": "DOUBLE PRECISION"},
        ],
    }
    entry.update(overrides)
    return entry


def slice_(datasets=None):
    return schema_slice.SchemaSlice.from_payload(
        {"datasets": datasets if datasets is not None else [dataset()]}
    )


def check(sql, schema=None, dialect="postgres"):
    return sql_semantics.validate(sql, schema=schema or slice_(), dialect=dialect)


class CleanStatementTest(unittest.TestCase):
    def test_a_plain_select_is_accepted(self) -> None:
        self.assertTrue(
            check('select student_id, student_name from "Student Overview"').ok
        )

    def test_a_grouped_aggregate_is_accepted(self) -> None:
        for sql in (
            'select completion_status, count(*) from "Student Overview" group by 1',
            'select completion_status, count(*) from "Student Overview" '
            "group by completion_status",
            'select completion_status as status, count(*) from "Student Overview" '
            "group by status",
            'select s.student_id, count(*) from "Student Overview" s group by s.student_id',
        ):
            with self.subTest(sql=sql):
                self.assertTrue(check(sql).ok, check(sql).message())

    def test_a_numeric_aggregate_is_accepted(self) -> None:
        for sql in (
            'select sum(assessment_score) from "Student Overview"',
            'select avg(progress) from "Student Overview"',
            'select count(*) from "Student Overview"',
            'select count(student_id) from "Student Overview"',
        ):
            with self.subTest(sql=sql):
                self.assertTrue(check(sql).ok, check(sql).message())

    def test_select_star_is_not_judged(self) -> None:
        self.assertTrue(check('select * from "Student Overview"').ok)

    def test_a_cte_wrapper_is_not_judged_for_columns(self) -> None:
        self.assertTrue(
            check('with t as (select * from "Student Overview") select count(*) from t').ok
        )

    def test_an_alias_reference_in_order_by_is_not_a_column(self) -> None:
        self.assertTrue(
            check(
                'select student_id as sid from "Student Overview" order by sid'
            ).ok
        )


class UnknownColumnTest(unittest.TestCase):
    def test_an_invented_column_is_refused(self) -> None:
        result = check('select nonsense from "Student Overview"')

        self.assertEqual(result.code, sql_semantics.UNKNOWN_COLUMN)
        self.assertIn("nonsense", result.message())
        self.assertIn("Student Overview", result.message())

    def test_a_qualified_invented_column_is_refused(self) -> None:
        result = check('select s.nonsense from "Student Overview" s')

        self.assertEqual(result.code, sql_semantics.UNKNOWN_COLUMN)

    def test_an_unknown_qualifier_is_left_to_the_guard(self) -> None:
        """An unresolvable alias may be a subquery or CTE: not this module's call."""
        result = check('select x.whatever from (select 1 as whatever) x')

        self.assertTrue(result.ok)

    def test_two_datasets_do_not_judge_unqualified_columns(self) -> None:
        schema = slice_(
            [
                dataset(),
                dataset(name="Course Analytics", columns=[{"name": "course_id"}]),
            ]
        )

        result = check(
            'select anything from "Student Overview" a join "Course Analytics" c '
            "on a.student_id = c.course_id",
            schema,
        )

        self.assertTrue(result.ok)


class AggregateTypeTest(unittest.TestCase):
    def test_summing_a_string_is_refused(self) -> None:
        result = check('select sum(completion_status) from "Student Overview"')

        self.assertEqual(result.code, sql_semantics.NON_NUMERIC_AGGREGATE)
        self.assertIn("STRING", result.message())

    def test_an_untyped_column_passes(self) -> None:
        schema = slice_([dataset(columns=[{"name": "score"}])])

        self.assertTrue(check('select avg(score) from "Student Overview"', schema).ok)

    def test_an_expression_argument_is_not_judged(self) -> None:
        self.assertTrue(
            check('select sum(assessment_score * 2) from "Student Overview"').ok
        )


class GroupingTest(unittest.TestCase):
    def test_an_ungrouped_column_beside_an_aggregate_is_refused(self) -> None:
        result = check('select student_id, count(*) from "Student Overview"')

        self.assertEqual(result.code, sql_semantics.UNGROUPED_COLUMN)
        self.assertIn("student_id", result.message())

    def test_only_aggregates_is_fine(self) -> None:
        self.assertTrue(
            check('select count(*), sum(assessment_score) from "Student Overview"').ok
        )

    def test_a_star_select_is_not_grouping_judged(self) -> None:
        self.assertTrue(check('select *, count(*) from "Student Overview"').ok)


class ErrorTest(unittest.TestCase):
    def test_unparseable_sql_raises_rather_than_reporting_an_issue(self) -> None:
        with self.assertRaises(sql_semantics.SemanticsError) as raised:
            check("select from where")

        self.assertEqual(raised.exception.code, "parse_error")

    def test_a_stacked_statement_is_refused_by_the_parser(self) -> None:
        with self.assertRaises(sql_semantics.SemanticsError) as raised:
            check('select 1 from "Student Overview"; drop table x')

        self.assertEqual(raised.exception.code, "multiple_statements")

    def test_issues_are_bounded(self) -> None:
        columns = ", ".join(f"missing_{index}" for index in range(20))

        result = check(f'select {columns} from "Student Overview"')

        self.assertEqual(len(result.issues), sql_semantics.MAX_ISSUES)

    def test_the_public_payload_is_content_free_metadata(self) -> None:
        payload = check('select nonsense from "Student Overview"').to_public_dict()

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], sql_semantics.UNKNOWN_COLUMN)
        self.assertEqual(set(payload["issues"][0]), {"code", "detail"})


if __name__ == "__main__":
    unittest.main()
