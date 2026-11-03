"""Adversarial corpus for the read-only SQL guard (P4-F05).

The phase's acceptance evidence is "adversarial test corpus passes", so this file
is the corpus: every case is a real attempt a model (or a user, through a model)
could make, paired with the exact refusal code it must produce.
"""

from __future__ import annotations

import unittest

from xp_prompt import sql_guard

ALLOWED_TABLES = {
    "public.mdl_user",
    "public.mdl_course",
    "mdl_user",
    "mdl_course",
}

ALLOWED_CASES: tuple[str, ...] = (
    "select count(*) from mdl_user",
    "SELECT u.id, count(*) FROM public.mdl_user u GROUP BY u.id ORDER BY 2 DESC",
    "with active as (select id from mdl_user) select count(*) from active",
    "select * from (select id from mdl_user) t",
    "select a.id from mdl_user a join mdl_course c on c.id = a.id",
    "select * from mdl_user where id in (select userid from mdl_course)",
    "select 1",
)

DENIED_CASES: tuple[tuple[str, str], ...] = (
    # statement stacking and obfuscation
    ("select 1; drop table mdl_user", "multiple_statements"),
    ("select 1;select 2", "multiple_statements"),
    ("select 1 /*! ; drop table x */", "obfuscation"),
    # write statements
    ("delete from mdl_user", "not_read_only"),
    ("update mdl_user set id = 1", "not_read_only"),
    ("insert into mdl_user values (1)", "not_read_only"),
    ("create table x (a int)", "not_read_only"),
    ("drop table mdl_user", "not_read_only"),
    ("truncate table mdl_user", "not_read_only"),
    ("grant select on mdl_user to public", "not_read_only"),
    ("copy mdl_user to '/tmp/x'", "not_read_only"),
    ("merge into mdl_user using mdl_course on true when matched then delete", "not_read_only"),
    ("select * into new_table from mdl_user", "not_read_only"),
    # administrative statements parsed as opaque commands
    ("show tables", "not_read_only"),
    ("explain select 1", "not_read_only"),
    ("set search_path = public", "not_read_only"),
    # unsafe functions
    ("select pg_read_file('/etc/passwd')", "unsafe_function"),
    ("select pg_read_binary_file('/etc/passwd')", "unsafe_function"),
    ("select lo_import('/etc/passwd')", "unsafe_function"),
    ("select pg_sleep(10)", "unsafe_function"),
    ("select nextval('user_seq')", "unsafe_function"),
    ("select setval('user_seq', 1)", "unsafe_function"),
    ("select * from dblink('host=x', 'select 1') as t(a int)", "unsafe_function"),
    ("select load_file('/etc/passwd')", "unsafe_function"),
    ("select read_csv('/etc/passwd')", "unsafe_function"),
    ("select benchmark(100000000, md5('x'))", "unsafe_function"),
    # metadata schemas
    ("select * from information_schema.tables", "system_schema"),
    ("select * from pg_catalog.pg_user", "system_schema"),
    ("select * from mysql.user", "system_schema"),
    ("select * from performance_schema.threads", "system_schema"),
    # authorization
    ("select * from mdl_secret", "unknown_table"),
    ("select * from other_db.mdl_user", "unknown_table"),
    # input hygiene
    ("", "empty"),
    ("   ", "empty"),
)


class AllowedQueryTest(unittest.TestCase):
    def test_allowed_queries_are_rewritten_with_a_cap(self) -> None:
        for sql in ALLOWED_CASES:
            with self.subTest(sql=sql):
                result = sql_guard.check(sql, allowed_tables=ALLOWED_TABLES, max_rows=100)
                self.assertTrue(result.read_only)
                self.assertLessEqual(result.limit, 100)
                self.assertIn("LIMIT", result.sql.upper())

    def test_the_original_text_is_never_returned(self) -> None:
        result = sql_guard.check(
            "select count(*) from mdl_user", allowed_tables=ALLOWED_TABLES
        )

        self.assertNotEqual(result.sql, "select count(*) from mdl_user")

    def test_cte_names_are_not_treated_as_tables(self) -> None:
        result = sql_guard.check(
            "with active as (select id from mdl_user) select * from active",
            allowed_tables=ALLOWED_TABLES,
        )

        self.assertEqual(result.tables, ("mdl_user",))

    def test_tables_are_reported_for_audit(self) -> None:
        result = sql_guard.check(
            "select a.id from mdl_user a join mdl_course c on c.id = a.id",
            allowed_tables=ALLOWED_TABLES,
        )

        self.assertEqual(result.tables, ("mdl_course", "mdl_user"))


class ConnectionLabelTest(unittest.TestCase):
    """A Superset connection name is metadata, never a SQL catalog (P4-F23).

    Live evidence: a question about a dataset on the ``File uploads`` connection
    produced ``FROM "File uploads".public."Student Overview"``, which PostgreSQL
    refuses with ``feature_not_supported: cross-database references are not
    implemented``. The label has to be removed before execution.
    """

    def test_a_connection_label_is_stripped_from_the_reference(self) -> None:
        result = sql_guard.check(
            'SELECT COUNT(*) FROM "File uploads".public."Student Overview"',
            allowed_tables={'public.student overview'},
            connection_names=('File uploads',),
        )

        self.assertEqual(result.tables, ('public.student overview',))
        self.assertNotIn('File uploads', result.sql)
        self.assertIn('Student Overview', result.sql)

    def test_the_label_is_only_stripped_when_it_is_known(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError) as raised:
            sql_guard.check(
                'SELECT COUNT(*) FROM "File uploads".public."Student Overview"',
                allowed_tables={'public.student overview'},
            )

        self.assertEqual(raised.exception.code, 'unknown_table')

    def test_a_real_catalog_is_kept(self) -> None:
        result = sql_guard.check(
            'select * from proj.ds.orders',
            dialect='bigquery',
            allowed_tables={'proj.ds.orders', 'orders'},
            connection_names=('File uploads',),
        )

        self.assertEqual(result.tables, ('proj.ds.orders',))

    def test_a_label_cannot_widen_the_authorized_set(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError) as raised:
            sql_guard.check(
                'select * from "File uploads".public.secret_table',
                allowed_tables={'public.student overview'},
                connection_names=('File uploads',),
            )

        self.assertEqual(raised.exception.code, 'unknown_table')

    def test_the_case_of_the_label_does_not_matter(self) -> None:
        result = sql_guard.check(
            'select * from "file uploads".public."Student Overview"',
            allowed_tables={'public.student overview'},
            connection_names=('FILE UPLOADS',),
        )

        self.assertEqual(result.tables, ('public.student overview',))


class VirtualDatasetTest(unittest.TestCase):
    """A Superset SQL dataset has no physical table (P4-F24).

    Live evidence: every authorized dataset on the deployment was virtual, so
    ``FROM public."Student Overview"`` failed with ``UndefinedTable``. The dataset
    name is a governed query, and it has to be inlined as a CTE.
    """

    DATASET_SQL = 'SELECT student_id, student_name FROM public.users'

    def check(self, sql: str, **overrides):
        arguments = {
            'allowed_tables': {'student overview'},
            'virtual_tables': {'Student Overview': self.DATASET_SQL},
        }
        arguments.update(overrides)
        return sql_guard.check(sql, **arguments)

    def test_the_dataset_query_is_inlined_as_a_cte(self) -> None:
        result = self.check('SELECT COUNT(*) FROM "Student Overview"')

        self.assertEqual(result.inlined, ('student overview',))
        self.assertEqual(result.tables, ('student overview',))
        self.assertIn('WITH', result.sql.upper())
        self.assertIn('public.users', result.sql)
        self.assertIn('LIMIT', result.sql.upper())

    def test_a_qualified_or_miscased_reference_is_normalised(self) -> None:
        result = self.check('SELECT COUNT(*) FROM public."student overview" so')

        self.assertIn('WITH "Student Overview" AS', result.sql)
        self.assertIn('FROM "Student Overview" AS so', result.sql)

    def test_the_dataset_cte_comes_before_the_query_ctes(self) -> None:
        result = self.check(
            'WITH t AS (SELECT 1 AS a) SELECT s.student_id FROM "Student Overview" s'
        )

        self.assertLess(result.sql.index('"Student Overview" AS'), result.sql.index('t AS'))

    def test_two_virtual_datasets_both_inline(self) -> None:
        result = self.check(
            'SELECT * FROM "Student Overview" a JOIN "Course Analytics" c ON true',
            allowed_tables={'student overview', 'course analytics'},
            virtual_tables={
                'Student Overview': self.DATASET_SQL,
                'Course Analytics': 'SELECT course_id FROM public.courses',
            },
        )

        self.assertEqual(result.inlined, ('course analytics', 'student overview'))
        self.assertIn('public.courses', result.sql)

    def test_a_dataset_definition_that_is_not_a_query_is_refused(self) -> None:
        for definition in (
            'DELETE FROM public.users',
            'SELECT 1; DROP TABLE public.users',
            'SELECT pg_read_file(\'/etc/passwd\')',
        ):
            with self.subTest(definition=definition):
                with self.assertRaises(sql_guard.SqlPolicyError) as raised:
                    self.check(
                        'SELECT COUNT(*) FROM "Student Overview"',
                        virtual_tables={'Student Overview': definition},
                    )
                self.assertIn(
                    raised.exception.code,
                    {'virtual_dataset_invalid', 'unsafe_function'},
                )

    def test_an_unknown_table_beside_a_virtual_one_is_still_refused(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError) as raised:
            self.check(
                'SELECT * FROM "Student Overview" a JOIN public.secret s ON true'
            )

        self.assertEqual(raised.exception.code, 'unknown_table')

    def test_a_name_that_collides_with_a_query_cte_is_refused(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError) as raised:
            self.check(
                'WITH "Student Overview" AS (SELECT 1 AS a) '
                'SELECT * FROM "Student Overview"'
            )

        self.assertEqual(raised.exception.code, 'virtual_dataset_conflict')

    def test_a_physical_table_is_not_inlined(self) -> None:
        result = sql_guard.check(
            'select count(*) from public.mdl_user',
            allowed_tables={'public.mdl_user'},
            virtual_tables={'Student Overview': self.DATASET_SQL},
        )

        self.assertEqual(result.inlined, ())
        self.assertNotIn('WITH', result.sql.upper())


class LimitTest(unittest.TestCase):
    def test_missing_limit_is_added(self) -> None:
        result = sql_guard.check("select id from mdl_user", max_rows=25)

        self.assertEqual(result.limit, 25)
        self.assertFalse(result.wrapped)
        self.assertIn("LIMIT 25", result.sql)

    def test_oversized_limit_is_lowered(self) -> None:
        result = sql_guard.check("select id from mdl_user limit 1000000", max_rows=25)

        self.assertEqual(result.limit, 25)
        self.assertIn("LIMIT 25", result.sql)

    def test_small_limit_is_kept(self) -> None:
        result = sql_guard.check("select id from mdl_user limit 5", max_rows=25)

        self.assertEqual(result.limit, 5)
        self.assertIn("LIMIT 5", result.sql)

    def test_non_literal_limit_is_wrapped(self) -> None:
        result = sql_guard.check("select id from mdl_user limit ?", max_rows=25)

        self.assertTrue(result.wrapped)
        self.assertEqual(result.limit, 25)
        self.assertIn("xp_capped", result.sql)

    def test_zero_or_negative_cap_falls_back_to_one(self) -> None:
        result = sql_guard.check("select id from mdl_user", max_rows=0)

        self.assertEqual(result.limit, sql_guard.MIN_ROWS)


class DeniedQueryTest(unittest.TestCase):
    def test_every_denied_case_reports_its_code(self) -> None:
        for sql, expected in DENIED_CASES:
            with self.subTest(sql=sql):
                with self.assertRaises(sql_guard.SqlPolicyError) as raised:
                    sql_guard.check(sql, allowed_tables=ALLOWED_TABLES)
                self.assertEqual(raised.exception.code, expected)
                self.assertNotIn("mdl_user", str(raised.exception))

    def test_oversized_sql_is_rejected(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError) as raised:
            sql_guard.check("select " + "x" * sql_guard.MAX_SQL_CHARS)

        self.assertEqual(raised.exception.code, "too_large")

    def test_unparseable_sql_is_rejected(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError) as raised:
            sql_guard.check("select from where ((")

        self.assertEqual(raised.exception.code, "parse_error")

    def test_unknown_dialect_is_rejected(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError) as raised:
            sql_guard.check("select 1", dialect="oracle")

        self.assertEqual(raised.exception.code, "unsupported_dialect")

    def test_an_empty_allowlist_denies_every_table(self) -> None:
        with self.assertRaises(sql_guard.SqlPolicyError) as raised:
            sql_guard.check("select * from mdl_user", allowed_tables=[])

        self.assertEqual(raised.exception.code, "unknown_table")

    def test_no_allowlist_means_no_table_restriction(self) -> None:
        result = sql_guard.check("select * from anything_at_all")

        self.assertEqual(result.tables, ("anything_at_all",))


class DialectTest(unittest.TestCase):
    def test_tsql_top_is_honoured(self) -> None:
        result = sql_guard.check("select top 10 id from mdl_user", dialect="tsql")

        self.assertEqual(result.limit, 10)
        self.assertIn("TOP 10", result.sql.upper())

    def test_bigquery_keeps_the_project_qualifier(self) -> None:
        result = sql_guard.check(
            "select id from `proj.ds.mdl_user`", dialect="bigquery"
        )

        self.assertEqual(result.tables, ("proj.ds.mdl_user",))

    def test_mysql_renders_its_own_dialect(self) -> None:
        result = sql_guard.check("select id from mdl_user", dialect="mysql")

        self.assertEqual(result.dialect, "mysql")
        self.assertIn("LIMIT", result.sql.upper())


class ResultShapeTest(unittest.TestCase):
    def test_public_dict_is_content_free_metadata(self) -> None:
        payload = sql_guard.check("select id from mdl_user").to_public_dict()

        self.assertEqual(
            sorted(payload),
            ["dialect", "inlined", "limit", "read_only", "sql", "tables", "wrapped"],
        )
        self.assertTrue(payload["read_only"])
        self.assertEqual(payload["inlined"], [])


if __name__ == "__main__":
    unittest.main()
