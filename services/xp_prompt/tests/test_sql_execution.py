"""Tests for execution-time re-validation (P4-F09)."""

from __future__ import annotations

import unittest

from xp_prompt import schema_slice, sql_execution, sql_guard


def slice_(datasets=None) -> schema_slice.SchemaSlice:
    return schema_slice.SchemaSlice.from_payload(
        {
            "datasets": datasets
            if datasets is not None
            else [
                {
                    "name": "mdl_user",
                    "schema": "public",
                    "database": "lms",
                    "dialect": "postgres",
                    "columns": [{"name": "id", "type": "int"}],
                }
            ]
        }
    )


class ResolveDialectTest(unittest.TestCase):
    def test_explicit_dialect_wins(self) -> None:
        self.assertEqual(
            sql_execution.resolve_dialect(slice_(), database="lms", dialect="mysql"),
            "mysql",
        )

    def test_database_selects_the_dialect(self) -> None:
        schema = slice_(
            [
                {"name": "a", "database": "lms", "dialect": "postgres"},
                {"name": "b", "database": "blog", "dialect": "mysql"},
            ]
        )

        self.assertEqual(
            sql_execution.resolve_dialect(schema, database="blog"), "mysql"
        )

    def test_several_dialects_without_a_target_is_ambiguous(self) -> None:
        schema = slice_(
            [
                {"name": "a", "database": "lms", "dialect": "postgres"},
                {"name": "b", "database": "blog", "dialect": "mysql"},
            ]
        )

        with self.assertRaises(sql_guard.SqlPolicyError) as raised:
            sql_execution.resolve_dialect(schema)

        self.assertEqual(raised.exception.code, "dialect_ambiguous")

    def test_unsupported_dialect_is_refused(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError) as raised:
            sql_execution.resolve_dialect(slice_(), dialect="oracle")

        self.assertEqual(raised.exception.code, "unsupported_dialect")

    def test_empty_slice_is_refused(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError):
            sql_execution.resolve_dialect(slice_([]))


class VirtualDatasetTest(unittest.TestCase):
    def test_a_virtual_dataset_is_inlined_before_execution(self) -> None:
        schema = slice_(
            [
                {
                    "name": "Student Overview",
                    "schema": "public",
                    "database": "File uploads",
                    "dialect": "postgresql",
                    "sql": "SELECT student_id FROM public.users",
                    "columns": [{"name": "student_id", "type": "int"}],
                }
            ]
        )

        result = sql_execution.validate(
            'SELECT COUNT(*) FROM "Student Overview"', schema=schema
        )

        self.assertEqual(result["inlined"], ["student overview"])
        self.assertIn("WITH", result["sql"].upper())
        self.assertIn("public.users", result["sql"])


class LabelQualifiedReferenceTest(unittest.TestCase):
    def test_a_connection_label_is_removed_before_execution(self) -> None:
        """The statement that runs must not carry the Superset connection name."""
        schema = slice_(
            [
                {
                    "name": "Student Overview",
                    "schema": "public",
                    "database": "File uploads",
                    "dialect": "postgresql",
                    "columns": [{"name": "id", "type": "int"}],
                }
            ]
        )

        result = sql_execution.validate(
            'SELECT COUNT(*) FROM "File uploads".public."Student Overview"',
            schema=schema,
        )

        self.assertNotIn("File uploads", result["sql"])
        self.assertEqual(result["tables"], ["public.student overview"])


class ValidateTest(unittest.TestCase):
    def test_allowed_statement_is_returned_rewritten(self) -> None:
        result = sql_execution.validate(
            "select id from public.mdl_user", schema=slice_(), max_rows=50
        )

        self.assertIn("LIMIT 50", result["sql"])
        self.assertEqual(result["tables"], ["public.mdl_user"])
        self.assertEqual(result["limit"], 50)
        self.assertEqual(result["schema_fingerprint"], slice_().fingerprint)

    def test_unauthorized_table_is_refused_at_execution_time(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError) as raised:
            sql_execution.validate(
                "select id from public.mdl_secret", schema=slice_()
            )

        self.assertEqual(raised.exception.code, "unknown_table")

    def test_write_statement_is_refused_at_execution_time(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError) as raised:
            sql_execution.validate("delete from public.mdl_user", schema=slice_())

        self.assertEqual(raised.exception.code, "not_read_only")

    def test_stacked_statement_is_refused_at_execution_time(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError) as raised:
            sql_execution.validate(
                "select 1; drop table public.mdl_user", schema=slice_()
            )

        self.assertEqual(raised.exception.code, "multiple_statements")

    def test_the_returned_sql_is_never_the_input_text(self) -> None:
        result = sql_execution.validate(
            "select id from mdl_user", schema=slice_(), max_rows=10
        )

        self.assertNotEqual(result["sql"], "select id from mdl_user")


if __name__ == "__main__":
    unittest.main()
