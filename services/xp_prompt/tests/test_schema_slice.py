"""Tests for the authorized schema slice (P4-F02)."""

from __future__ import annotations

import unittest

from xp_prompt import schema_slice


def dataset(**overrides):
    entry = {
        "dataset_id": 1,
        "name": "mdl_user",
        "schema": "public",
        "database": "lms",
        "dialect": "postgresql",
        "columns": [
            {"name": "id", "type": "int", "temporal": False},
            {"name": "deleted", "type": "int"},
        ],
    }
    entry.update(overrides)
    return entry


class NormalizationTest(unittest.TestCase):
    def test_engine_aliases_are_normalised(self) -> None:
        for raw, expected in (
            ("postgresql", "postgres"),
            ("postgresql+psycopg2", "postgres"),
            ("mysql://", "mysql"),
            ("mariadb", "mysql"),
            ("mssql", "tsql"),
        ):
            with self.subTest(dialect=raw):
                slice_ = schema_slice.SchemaSlice.from_payload(
                    {"datasets": [dataset(dialect=raw)]}
                )
                self.assertEqual(slice_.datasets[0].dialect, expected)

    def test_unknown_dialects_are_dropped(self) -> None:
        slice_ = schema_slice.SchemaSlice.from_payload(
            {"datasets": [dataset(dialect="oracle")]}
        )

        self.assertEqual(slice_.datasets, ())
        self.assertEqual(slice_.dropped, 1)

    def test_unsafe_names_are_dropped_not_escaped(self) -> None:
        for name in (
            'mdl_user" ; drop table x --',
            "mdl_user\nwhere 1=1",
            "mdl_user;",
            "",
            "x" * (schema_slice.MAX_NAME_CHARS + 1),
        ):
            with self.subTest(name=name):
                slice_ = schema_slice.SchemaSlice.from_payload(
                    {"datasets": [dataset(name=name)]}
                )
                self.assertEqual(slice_.datasets, ())

    def test_unsafe_columns_are_dropped(self) -> None:
        slice_ = schema_slice.SchemaSlice.from_payload(
            {
                "datasets": [
                    dataset(
                        columns=[
                            {"name": "ok"},
                            {"name": "bad" + "\n" + "col"},
                            {"name": ""},
                        ]
                    )
                ]
            }
        )

        self.assertEqual(
            [column.name for column in slice_.datasets[0].columns], ["ok"]
        )

    def test_duplicate_datasets_collapse(self) -> None:
        slice_ = schema_slice.SchemaSlice.from_payload(
            {"datasets": [dataset(), dataset(dataset_id=2)]}
        )

        self.assertEqual(len(slice_.datasets), 1)
        self.assertEqual(slice_.dropped, 1)

    def test_bounds_are_enforced(self) -> None:
        many = [
            dataset(
                name=f"table_{index}",
                columns=[{"name": f"c{column}"} for column in range(100)],
            )
            for index in range(schema_slice.MAX_DATASETS + 10)
        ]

        slice_ = schema_slice.SchemaSlice.from_payload({"datasets": many})

        self.assertEqual(len(slice_.datasets), schema_slice.MAX_DATASETS)
        self.assertEqual(
            len(slice_.datasets[0].columns), schema_slice.MAX_COLUMNS
        )

    def test_non_list_payload_is_an_empty_slice(self) -> None:
        for raw in (None, {}, "text", 7):
            self.assertEqual(schema_slice.SchemaSlice.from_payload(raw).datasets, ())


class FingerprintTest(unittest.TestCase):
    def test_same_slice_same_fingerprint(self) -> None:
        first = schema_slice.SchemaSlice.from_payload({"datasets": [dataset()]})
        second = schema_slice.SchemaSlice.from_payload({"datasets": [dataset()]})

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.fingerprint), 64)

    def test_input_order_does_not_change_the_fingerprint(self) -> None:
        other = dataset(
            dataset_id=2, name="mdl_course", columns=[{"name": "id"}]
        )

        forward = schema_slice.SchemaSlice.from_payload(
            {"datasets": [dataset(), other]}
        )
        backward = schema_slice.SchemaSlice.from_payload(
            {"datasets": [other, dataset()]}
        )

        self.assertEqual(forward.fingerprint, backward.fingerprint)

    def test_a_changed_column_changes_the_fingerprint(self) -> None:
        first = schema_slice.SchemaSlice.from_payload({"datasets": [dataset()]})
        second = schema_slice.SchemaSlice.from_payload(
            {"datasets": [dataset(columns=[{"name": "id", "type": "bigint"}])]}
        )

        self.assertNotEqual(first.fingerprint, second.fingerprint)


class AllowedTablesTest(unittest.TestCase):
    def test_every_reference_form_is_accepted(self) -> None:
        slice_ = schema_slice.SchemaSlice.from_payload({"datasets": [dataset()]})

        self.assertEqual(
            slice_.allowed_tables(),
            frozenset({"mdl_user", "public.mdl_user", "lms.public.mdl_user"}),
        )

    def test_a_database_filter_narrows_the_set(self) -> None:
        slice_ = schema_slice.SchemaSlice.from_payload(
            {
                "datasets": [
                    dataset(),
                    dataset(name="wp_post", schema="", database="blog"),
                ]
            }
        )

        self.assertIn("wp_post", slice_.allowed_tables())
        self.assertNotIn("wp_post", slice_.allowed_tables(database="lms"))

    def test_the_guard_accepts_what_the_slice_allows(self) -> None:
        from xp_prompt import sql_guard

        slice_ = schema_slice.SchemaSlice.from_payload({"datasets": [dataset()]})

        result = sql_guard.check(
            "select id from public.mdl_user",
            dialect="postgres",
            allowed_tables=slice_.allowed_tables(),
        )
        self.assertEqual(result.tables, ("public.mdl_user",))

        with self.assertRaises(sql_guard.SqlPolicyError):
            sql_guard.check(
                "select id from public.mdl_secret",
                dialect="postgres",
                allowed_tables=slice_.allowed_tables(),
            )


class VirtualDatasetTest(unittest.TestCase):
    def test_a_virtual_dataset_is_referenced_by_its_bare_name(self) -> None:
        slice_ = schema_slice.SchemaSlice.from_payload(
            {"datasets": [dataset(sql="SELECT id FROM public.users")]}
        )

        entry = slice_.datasets[0]
        self.assertTrue(entry.is_virtual)
        self.assertEqual(entry.sql_name, "mdl_user")
        self.assertNotIn("public.mdl_user", slice_.allowed_tables())
        self.assertIn("mdl_user", slice_.allowed_tables())
        self.assertIn("dataset", entry.render())

    def test_the_definition_is_exposed_for_inlining(self) -> None:
        slice_ = schema_slice.SchemaSlice.from_payload(
            {
                "datasets": [
                    dataset(sql="SELECT id FROM public.users"),
                    dataset(name="wp_post", database="blog"),
                ]
            }
        )

        self.assertEqual(
            slice_.virtual_tables(), {"mdl_user": "SELECT id FROM public.users"}
        )
        self.assertEqual(slice_.virtual_tables(database="blog"), {})

    def test_the_inlining_key_keeps_the_dataset_spelling(self) -> None:
        slice_ = schema_slice.SchemaSlice.from_payload(
            {"datasets": [dataset(name="Student Overview", sql="SELECT 1 AS a")]}
        )

        self.assertEqual(list(slice_.virtual_tables()), ["Student Overview"])

    def test_a_templated_definition_is_dropped(self) -> None:
        slice_ = schema_slice.SchemaSlice.from_payload(
            {
                "datasets": [
                    dataset(sql="SELECT * FROM {{ filter_values('x') }}"),
                ]
            }
        )

        self.assertEqual(slice_.virtual_tables(), {})
        self.assertFalse(slice_.datasets[0].is_virtual)

    def test_a_changed_definition_changes_the_fingerprint(self) -> None:
        first = schema_slice.SchemaSlice.from_payload(
            {"datasets": [dataset(sql="SELECT id FROM public.users")]}
        )
        second = schema_slice.SchemaSlice.from_payload(
            {"datasets": [dataset(sql="SELECT id, deleted FROM public.users")]}
        )

        self.assertNotEqual(first.fingerprint, second.fingerprint)


class PromptTest(unittest.TestCase):
    def test_prompt_lists_only_validated_names(self) -> None:
        slice_ = schema_slice.SchemaSlice.from_payload(
            {"datasets": [dataset(), dataset(name="bad name; drop", schema="public")]}
        )

        prompt = slice_.prompt_text()

        self.assertIn("public.mdl_user", prompt)
        self.assertIn("id int", prompt)
        self.assertIn("deleted int", prompt)
        self.assertNotIn("drop", prompt)

    def test_the_connection_is_a_label_not_a_qualifier(self) -> None:
        """A prompt that teaches ``connection.schema.table`` produces SQL that
        PostgreSQL refuses (cross-database reference), so the label is reported
        as metadata and the reference starts with the schema."""
        slice_ = schema_slice.SchemaSlice.from_payload({"datasets": [dataset()]})

        prompt = slice_.prompt_text()

        self.assertIn("public.mdl_user", prompt)
        self.assertIn("connection: lms", prompt)
        self.assertNotIn("lms.public.mdl_user", prompt)

    def test_connection_names_are_exposed_for_the_guard(self) -> None:
        slice_ = schema_slice.SchemaSlice.from_payload(
            {
                "datasets": [
                    dataset(),
                    dataset(name="wp_post", database="blog"),
                ]
            }
        )

        self.assertEqual(slice_.connection_names(), ("blog", "lms"))

    def test_prompt_is_bounded(self) -> None:
        datasets = [
            dataset(
                name=f"table_{index}",
                columns=[{"name": f"column_{column}", "type": "text"} for column in range(60)],
            )
            for index in range(schema_slice.MAX_DATASETS)
        ]
        slice_ = schema_slice.SchemaSlice.from_payload({"datasets": datasets})

        prompt = slice_.prompt_text()

        self.assertLessEqual(len(prompt), schema_slice.MAX_PROMPT_CHARS + 40)
        self.assertIn("additional tables omitted", prompt)

    def test_public_dict_hides_the_prompt_by_default(self) -> None:
        slice_ = schema_slice.SchemaSlice.from_payload({"datasets": [dataset()]})

        self.assertNotIn("prompt", slice_.to_public_dict())
        self.assertIn("prompt", slice_.to_public_dict(include_prompt=True))

    def test_public_dict_reports_counts_and_tables(self) -> None:
        payload = schema_slice.SchemaSlice.from_payload(
            {"datasets": [dataset(), dataset(name="mdl_course")]}
        ).to_public_dict()

        self.assertEqual(payload["dataset_count"], 2)
        self.assertEqual(payload["dialects"], ["postgres"])
        self.assertEqual(
            [table["name"] for table in payload["tables"]],
            ["public.mdl_course", "public.mdl_user"],
        )


if __name__ == "__main__":
    unittest.main()
