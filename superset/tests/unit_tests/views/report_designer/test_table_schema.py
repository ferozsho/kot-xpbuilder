# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Unit tests for cross-dialect SyncDb staging-table DDL."""

from superset.views.report_designer.table_schema import (
    _column_sql,
    _ddl,
    _postgres_index_sql,
    _postgres_type_sql,
)


def test_postgres_type_mapping_covers_moodle_mariadb_types() -> None:
    assert _postgres_type_sql("bigint(10)") == "BIGINT"
    assert _postgres_type_sql("mediumint(6)") == "INTEGER"
    assert _postgres_type_sql("tinyint(1)") == "SMALLINT"
    assert _postgres_type_sql("decimal(38,19)") == "NUMERIC(38,19)"
    assert _postgres_type_sql("float(11,4)") == "REAL"
    assert _postgres_type_sql("varchar(1333)") == "VARCHAR(1333)"
    assert _postgres_type_sql("longtext") == "TEXT"


def test_postgres_column_omits_mysql_auto_increment() -> None:
    sql = _column_sql(
        {
            "name": "id",
            "type": "bigint(10)",
            "nullable": False,
            "extra": "auto_increment",
        },
        "postgresql",
    )

    assert sql == '"id" BIGINT NOT NULL'


def test_postgres_create_table_has_no_mysql_options_or_inline_keys() -> None:
    ddl, warnings = _ddl(
        '"auditonekef"."mdl_user"',
        [
            {"name": "id", "type": "bigint(10)", "nullable": False},
            {"name": "username", "type": "varchar(100)", "nullable": False},
        ],
        ["id"],
        [
            {
                "name": "mdl_user_use_uix",
                "unique": True,
                "columns": ["username"],
            }
        ],
        "postgresql",
        True,
    )

    assert 'PRIMARY KEY ("id")' in ddl
    assert " ENGINE=" not in ddl
    assert 'KEY "mdl_user_use_uix"' not in ddl
    assert warnings == []


def test_postgres_secondary_index_uses_create_index() -> None:
    sql = _postgres_index_sql(
        '"auditonekef"."mdl_user"',
        {
            "name": "mdl_user_use_uix",
            "unique": True,
            "columns": ["username"],
        },
        {"id", "username"},
    )

    assert sql == (
        'CREATE UNIQUE INDEX "mdl_user_use_uix" '
        'ON "auditonekef"."mdl_user" ("username")'
    )
