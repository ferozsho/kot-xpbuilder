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
"""Create the target table before any rows are loaded.

A CSV carries rows only. Previously the first upload part created the table
through ``pandas.to_sql`` (inferred types, no primary key, no indexes) and the
indexes were bolted on afterwards. Three things went wrong with that:

* an empty source table was never uploaded at all, so the old copy of it stayed
  behind with stale rows (or did not exist);
* pandas guessed the column types, so a numeric Moodle column could land as
  ``text`` and a report's ``SUM()`` silently changed meaning;
* the primary key arrived minutes after the data, leaving the load unindexed.

The Moodle plugin therefore reads the real definitions from the source's
``information_schema`` and calls this module first: the table is created with
the same columns, the same types, the same primary key and the same secondary
indexes as the original, and only then are rows appended to it.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import inspect, text

from superset.models.core import Database

logger = logging.getLogger(__name__)

#: A column type as MySQL's information_schema reports it, e.g. ``int(10)
#: unsigned``, ``varchar(255)``, ``decimal(10,2)``, ``enum('a','b')``.
TYPE_RE = re.compile(
    r"^[a-z][a-z0-9_ ]*"  # base type, may contain a space (double precision)
    r"(\([A-Za-z0-9_,'\" -]{0,255}\))?"  # optional length / enum members
    r"( unsigned)?( zerofill)?$",
    re.IGNORECASE,
)

#: A literal default value that is safe to inline in a DDL statement.
DEFAULT_RE = re.compile(r"^(?:[A-Za-z0-9_.:+-]{1,80}|'[^'\\]{0,100}'|NULL)$")
EXPRESSION_DEFAULT_RE = re.compile(
    r"^(?:CURRENT_TIMESTAMP|current_timestamp)(\(\d?\))?$", re.IGNORECASE
)

def _quote(identifier: str, dialect: str) -> str:
    """Quote one identifier for the target dialect."""
    clean = re.sub(r"[^A-Za-z0-9_]", "", identifier or "")
    if dialect in ("mysql", "mariadb"):
        return f"`{clean}`"
    return f'"{clean}"'


def _type_sql(raw: Any) -> str:
    """Validate a source column type before it reaches a DDL statement."""
    value = str(raw or "").strip()
    if not value or not TYPE_RE.match(value):
        raise ValueError(f"unsupported column type: {value[:60]!r}")
    return value


def _default_sql(raw: Any) -> str:
    """Inline a source default, or nothing when it is not safely expressible."""
    value = str(raw or "").strip()
    if not value:
        return ""
    if EXPRESSION_DEFAULT_RE.match(value):
        return f" DEFAULT {value}"
    if DEFAULT_RE.match(value):
        return f" DEFAULT {value}"
    return ""


def _column_sql(column: dict[str, Any], dialect: str) -> str:
    """One column definition, mirroring the source as closely as possible."""
    name = _quote(str(column.get("name") or ""), dialect)
    sql = f"{name} {_type_sql(column.get('type'))}"
    extra = str(column.get("extra") or "").lower()
    # information_schema returns the whole extra clause ("on update
    # CURRENT_TIMESTAMP" and friends); only the one flag that changes insert
    # behaviour is carried over, the rest is cosmetic for a loaded copy.
    if "auto_increment" in extra:
        sql += " NOT NULL AUTO_INCREMENT"
        return sql
    sql += " NOT NULL" if not column.get("nullable", True) else " NULL"
    sql += _default_sql(column.get("default"))
    return sql


def _existing_signature(engine, table: str, schema: str | None) -> tuple | None:
    """Shape of the table already on the target, or None when it is absent."""
    inspector = inspect(engine)
    if not inspector.has_table(table, schema=schema):
        return None
    columns = []
    for column in inspector.get_columns(table, schema=schema):
        columns.append(
            (
                str(column["name"]),
                str(column["type"]).lower().replace(" ", ""),
                bool(column.get("nullable", True)),
            )
        )
    primary = (inspector.get_pk_constraint(table, schema=schema) or {}).get(
        "constrained_columns"
    ) or []
    indexes = []
    for index in inspector.get_indexes(table, schema=schema):
        indexes.append(
            (
                str(index.get("name")),
                bool(index.get("unique")),
                tuple(str(column) for column in index.get("column_names") or []),
            )
        )
    return (
        tuple(columns),
        tuple(str(name) for name in primary),
        tuple(sorted(indexes)),
    )


def _wanted_signature(columns: list[dict[str, Any]], primary: list[str],
                      indexes: list[dict[str, Any]]) -> tuple:
    """Shape the plugin asked for, in the same form the inspector returns."""
    wanted_columns = []
    for column in columns:
        wanted_columns.append(
            (
                str(column.get("name")),
                _type_sql(column.get("type")).lower().replace(" ", ""),
                bool(column.get("nullable", True)),
            )
        )
    wanted_indexes = []
    for index in indexes:
        wanted_indexes.append(
            (
                str(index.get("name")),
                bool(index.get("unique")),
                tuple(str(column) for column in index.get("columns") or []),
            )
        )
    return (tuple(wanted_columns), tuple(str(name) for name in primary),
            tuple(sorted(wanted_indexes)))


def _table_options(collation: str | None) -> str:
    """CREATE TABLE options, keeping the source collation where it is safe.

    Report SQL groups and orders text columns, so a collation that differs from
    Moodle's can quietly change a result. Only utf8mb4 collations that MariaDB
    also ships are used: MySQL 8's ``utf8mb4_0900_*`` family does not exist
    there and would make the whole statement fail.
    """
    options = " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    name = str(collation or "").strip()
    if not name.startswith("utf8mb4_") or "_0900_" in name:
        return options
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name):
        return options
    return f"{options} COLLATE={name}"


def _ddl(qualified: str, columns: list[dict[str, Any]], primary: list[str],
         indexlist: list[dict[str, Any]], dialect: str, create_indexes: bool,
         collation: str | None = None) -> str:
    """The CREATE TABLE statement for one staging table."""
    definitions = [_column_sql(column, dialect) for column in columns]
    known = {str(column.get("name")) for column in columns}
    keycolumns = [name for name in primary if name in known]
    if keycolumns:
        columnlist = ", ".join(_quote(name, dialect) for name in keycolumns)
        definitions.append(f"PRIMARY KEY ({columnlist})")
    if create_indexes:
        for spec in indexlist:
            if spec.get("primary"):
                continue
            # An index may only name columns the table has, otherwise MySQL
            # refuses the whole CREATE TABLE ("Key column doesn't exist").
            names = [
                str(column)
                for column in spec.get("columns") or []
                if str(column) in known
            ]
            if not names:
                continue
            unique = "UNIQUE " if spec.get("unique") else ""
            columnlist = ", ".join(_quote(name, dialect) for name in names)
            indexname = _quote(str(spec.get("name") or names[0]), dialect)
            definitions.append(f"{unique}KEY {indexname} ({columnlist})")
    body = ",\n  ".join(definitions)
    return (
        f"CREATE TABLE {qualified} (\n  {body}\n)" + _table_options(collation)
    )


def prepare_table(
    database: Database,
    table: str,
    schema: str | None,
    columns: list[dict[str, Any]],
    primary_key: list[str] | None = None,
    indexes: list[dict[str, Any]] | None = None,
    replace: bool = True,
    create_indexes: bool = True,
    collation: str | None = None,
) -> dict[str, Any]:
    """Create (or recreate) a staging table with the source's own definition.

    ``columns`` is what the Moodle plugin read from the source's
    ``information_schema``::

        [{"name": "id", "type": "bigint(10)", "nullable": false,
          "extra": "auto_increment"},
         {"name": "fullname", "type": "varchar(255)", "nullable": false}]

    ``replace=True`` drops a table that already exists, which is what a reset
    needs: rows of a table that is empty on the source must not survive from an
    older copy. With ``replace=False`` an existing table whose shape already
    matches is left alone.
    """
    clean = re.sub(r"[^a-z0-9_]", "_", (table or "").lower())
    if not clean:
        raise ValueError("table name is required")
    if not columns:
        raise ValueError("column definitions are required")

    primary = [str(name) for name in (primary_key or [])]
    indexlist = list(indexes or [])

    with database.get_sqla_engine(schema=schema) as engine:
        dialect = (engine.dialect.name or "").lower()
        if dialect not in ("mysql", "mariadb"):
            raise ValueError(f"unsupported target dialect: {dialect}")

        qualified = _quote(clean, dialect)
        if schema:
            qualified = f"{_quote(schema, dialect)}.{qualified}"

        existing = _existing_signature(engine, clean, schema)
        wanted = _wanted_signature(columns, primary, indexlist)
        secondary = len([spec for spec in indexlist if not spec.get("primary")])
        if existing is not None and not replace and existing == wanted:
            return {
                "table": clean,
                "created": False,
                "dropped": False,
                "unchanged": True,
                "columns": len(columns),
                "indexes": 0,
            }

        ddl = _ddl(qualified, columns, primary, indexlist, dialect, create_indexes,
                   collation)
        dropped = existing is not None
        with engine.begin() as connection:
            if dropped:
                connection.execute(text(f"DROP TABLE IF EXISTS {qualified}"))
            connection.execute(text(ddl))

    logger.info(
        "Prepared %s%s (%d columns, %d indexes, dropped=%s)",
        f"{schema}." if schema else "",
        clean,
        len(columns),
        secondary,
        dropped,
    )
    return {
        "table": clean,
        "created": True,
        "dropped": dropped,
        "unchanged": False,
        "columns": len(columns),
        "indexes": secondary,
    }
