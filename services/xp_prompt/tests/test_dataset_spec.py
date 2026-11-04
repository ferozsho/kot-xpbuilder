"""Tests for the dataset specification (P6-F02)."""

from __future__ import annotations

import unittest

from xp_prompt import dataset_spec, schema_slice

SQL = "select id, firstname, created_at, score from public.mdl_user"


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
                    "columns": [
                        {"name": "id", "type": "bigint"},
                        {"name": "firstname", "type": "text"},
                        {"name": "created_at", "type": "timestamp"},
                        {"name": "score", "type": "numeric"},
                    ],
                }
            ]
        }
    )


def virtual(**overrides):
    body = {
        "name": "users_by_month",
        "kind": "virtual",
        "database": "lms",
        "sql": SQL,
        "columns": [
            {"name": "id", "type": "bigint"},
            {"name": "firstname", "type": "varchar"},
            {"name": "created_at", "type": "timestamp"},
            {"name": "score", "type": "numeric"},
        ],
        "grain": ["id"],
        "metrics": [
            {"name": "Users", "expression": "COUNT(*)"},
            {"name": "Avg score", "expression": "AVG(score)"},
        ],
    }
    body.update(overrides)
    return body


class TypeVocabularyTest(unittest.TestCase):
    def test_driver_types_are_mapped(self) -> None:
        self.assertEqual(dataset_spec.normalise_type("character varying(255)"), "text")
        self.assertEqual(dataset_spec.normalise_type("timestamp with time zone"), "datetime")
        self.assertEqual(dataset_spec.normalise_type("int8"), "bigint")
        self.assertEqual(dataset_spec.normalise_type("nonstandard"), "other")

    def test_empty_type_is_other_not_a_guess(self) -> None:
        self.assertEqual(dataset_spec.normalise_type(""), "other")


class VirtualSpecTest(unittest.TestCase):
    def test_a_virtual_spec_carries_no_preview_cap(self) -> None:
        spec = dataset_spec.from_payload(virtual(), schema=slice_())

        self.assertEqual(spec.kind, dataset_spec.VIRTUAL)
        self.assertNotIn("LIMIT", spec.sql.upper())
        self.assertEqual(spec.dialect, "postgres")
        self.assertIn("public.mdl_user", spec.tables)

    def test_the_fingerprint_is_content_free_and_stable(self) -> None:
        first = dataset_spec.from_payload(virtual(), schema=slice_())
        second = dataset_spec.from_payload(virtual(), schema=slice_())

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.fingerprint), 64)
        self.assertNotIn("users_by_month", first.fingerprint)

    def test_a_different_definition_is_a_different_fingerprint(self) -> None:
        first = dataset_spec.from_payload(virtual(), schema=slice_())
        second = dataset_spec.from_payload(
            virtual(sql="select id, firstname from public.mdl_user"), schema=slice_()
        )
        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_types_are_normalised_on_the_columns(self) -> None:
        spec = dataset_spec.from_payload(virtual(), schema=slice_())

        self.assertEqual(spec.column("firstname").type, "text")
        self.assertEqual(spec.column("score").type, "decimal")
        self.assertEqual(spec.column("created_at").type, "datetime")

    def test_the_temporal_column_is_inferred(self) -> None:
        spec = dataset_spec.from_payload(virtual(), schema=slice_())
        self.assertEqual(spec.temporal_column, "created_at")
        self.assertNotIn(
            "no_temporal_column", [warning["code"] for warning in spec.warnings]
        )

    def test_a_missing_temporal_column_is_stated_not_hidden(self) -> None:
        spec = dataset_spec.from_payload(
            virtual(columns=[{"name": "id", "type": "bigint"}]), schema=slice_()
        )
        self.assertEqual(spec.temporal_column, "")
        self.assertIn("no_temporal_column", [warning["code"] for warning in spec.warnings])

    def test_a_declared_temporal_column_must_be_typed_temporally(self) -> None:
        spec = dataset_spec.from_payload(
            virtual(temporal_column="firstname"), schema=slice_()
        )
        self.assertEqual(spec.temporal_column, "firstname")
        self.assertIn(
            "temporal_type_mismatch", [warning["code"] for warning in spec.warnings]
        )

    def test_an_unknown_temporal_column_is_dropped_with_a_warning(self) -> None:
        spec = dataset_spec.from_payload(
            virtual(temporal_column="not_a_column"), schema=slice_()
        )
        self.assertEqual(spec.temporal_column, "")
        self.assertIn("unknown_temporal", [warning["code"] for warning in spec.warnings])


class RefusalTest(unittest.TestCase):
    def _refused(self, **overrides) -> str:
        with self.assertRaises(dataset_spec.DatasetSpecError) as caught:
            dataset_spec.from_payload(virtual(**overrides), schema=slice_())
        return caught.exception.code

    def test_a_destructive_definition_is_refused(self) -> None:
        code = self._refused(sql="delete from public.mdl_user")
        self.assertEqual(code, "sql_blocked:not_read_only")

    def test_an_unlisted_table_is_refused(self) -> None:
        code = self._refused(sql="select id from public.mdl_course")
        self.assertEqual(code, "sql_blocked:unknown_table")

    def test_two_statements_are_refused(self) -> None:
        code = self._refused(sql=f"{SQL}; drop table public.mdl_user")
        self.assertEqual(code, "sql_blocked:multiple_statements")

    def test_a_missing_name_is_refused(self) -> None:
        self.assertEqual(self._refused(name=""), "name_required")

    def test_an_identifier_shaped_name_is_required(self) -> None:
        self.assertEqual(self._refused(name="users; drop"), "invalid_name")

    def test_an_unknown_kind_is_refused(self) -> None:
        self.assertEqual(self._refused(kind="magic"), "unsupported_kind")

    def test_a_virtual_dataset_needs_sql(self) -> None:
        self.assertEqual(self._refused(sql=""), "sql_required")

    def test_a_virtual_dataset_must_not_name_a_table(self) -> None:
        self.assertEqual(self._refused(table="mdl_user"), "table_not_allowed")

    def test_a_physical_dataset_needs_a_table(self) -> None:
        code = self._refused(kind="physical", sql="", table="")
        self.assertEqual(code, "table_required")

    def test_a_physical_dataset_must_not_carry_sql(self) -> None:
        code = self._refused(kind="physical", table="mdl_user")
        self.assertEqual(code, "sql_not_allowed")

    def test_sql_cannot_be_validated_without_a_slice(self) -> None:
        with self.assertRaises(dataset_spec.DatasetSpecError) as caught:
            dataset_spec.from_payload(virtual())
        self.assertEqual(caught.exception.code, "schema_required")

    def test_semantics_are_checked_on_the_definition(self) -> None:
        code = self._refused(sql="select nonesuch from public.mdl_user")
        self.assertEqual(code, "sql_blocked:unknown_column")


class PhysicalSpecTest(unittest.TestCase):
    def test_an_authorized_table_is_accepted(self) -> None:
        spec = dataset_spec.from_payload(
            {
                "name": "mdl user",
                "kind": "physical",
                "database": "lms",
                "schema": "public",
                "table": "mdl_user",
                "columns": [{"name": "id", "type": "bigint"}],
            },
            schema=slice_(),
        )
        self.assertEqual(spec.kind, dataset_spec.PHYSICAL)
        self.assertEqual(spec.table, "mdl_user")
        self.assertEqual(spec.tables, ("public.mdl_user",))
        self.assertEqual(spec.grain, ())

    def test_an_unauthorized_table_is_refused(self) -> None:
        with self.assertRaises(dataset_spec.DatasetSpecError) as caught:
            dataset_spec.from_payload(
                {
                    "name": "secrets",
                    "kind": "physical",
                    "database": "lms",
                    "schema": "public",
                    "table": "mdl_course",
                },
                schema=slice_(),
            )
        self.assertEqual(caught.exception.code, "table_not_authorized")


class ColumnAndMetricTest(unittest.TestCase):
    def test_duplicate_columns_collapse_case_insensitively(self) -> None:
        spec = dataset_spec.from_payload(
            virtual(
                columns=[
                    {"name": "id", "type": "bigint"},
                    {"name": "ID", "type": "bigint"},
                ]
            ),
            schema=slice_(),
        )
        self.assertEqual(len(spec.columns), 1)

    def test_an_invalid_column_name_is_dropped(self) -> None:
        spec = dataset_spec.from_payload(
            virtual(columns=[{"name": "1 bad name", "type": "text"}]), schema=slice_()
        )
        self.assertEqual(spec.columns, ())

    def test_grain_must_name_real_columns_and_keeps_their_spelling(self) -> None:
        spec = dataset_spec.from_payload(
            virtual(grain=["id", "nonesuch", "ID"]), schema=slice_()
        )
        self.assertEqual(spec.grain, ("id",))

    def test_metrics_are_deduplicated_and_bounded(self) -> None:
        spec = dataset_spec.from_payload(
            virtual(
                metrics=[
                    {"name": "Users", "expression": "COUNT(*)"},
                    {"name": "users", "expression": "COUNT(id)"},
                    {"name": "", "expression": "SUM(score)"},
                ]
            ),
            schema=slice_(),
        )
        self.assertEqual(len(spec.metrics), 1)
        self.assertEqual(spec.metrics[0].expression, "COUNT(*)")

    def test_owners_are_deduplicated_and_bounded(self) -> None:
        spec = dataset_spec.from_payload(
            virtual(owners=["admin", "admin", "analyst"]), schema=slice_()
        )
        self.assertEqual(spec.owners, ("admin", "analyst"))

    def test_a_dataset_without_grain_or_metric_says_so(self) -> None:
        spec = dataset_spec.from_payload(
            virtual(grain=[], metrics=[]), schema=slice_()
        )
        self.assertIn(
            "no_grain_or_metric", [warning["code"] for warning in spec.warnings]
        )


class PublicShapeTest(unittest.TestCase):
    def test_the_statement_is_hidden_unless_requested(self) -> None:
        spec = dataset_spec.from_payload(virtual(), schema=slice_())
        hidden = spec.to_public_dict()
        self.assertTrue(hidden["sql_hidden"])
        self.assertNotIn("sql", hidden)
        self.assertIn("sql", spec.to_public_dict(include_sql=True))


if __name__ == "__main__":
    unittest.main()
