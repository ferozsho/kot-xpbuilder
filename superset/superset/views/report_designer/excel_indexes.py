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
"""Rebuild source indexes on a staged copy of a Moodle table.

The CSV carries rows only, so every staged table arrives without the indexes the
source had: pandas creates bare columns. Reports then join two multi-million row
tables with no access paths and fall back to full scans.

The Moodle plugin reads the real definitions from the source's
``information_schema`` and posts them here, so the copy keeps the same primary
key and the same secondary indexes as the original table.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import inspect, text

from superset.models.core import Database
from superset.views.report_designer.table_schema import index_definition

logger = logging.getLogger(__name__)


def _quote(identifier: str, dialect: str) -> str:
    """Quote one identifier for the target dialect."""
    clean = re.sub(r"[^A-Za-z0-9_]", "", identifier)
    if dialect in ("mysql", "mariadb"):
        return f"`{clean}`"
    return f'"{clean}"'


def _add_secondary_index(connection, qualified: str, spec: dict[str, Any],
                          coltypes: dict[str, str], dialect: str,
                          existing: set[str]) -> tuple[str, str]:
    """Create one secondary index, key-prefix safe.

    Returns ``("applied" | "skipped", note)``: ``note`` is the reason for a skip
    or the warning of an index that had to be shortened to fit.
    """
    name = str(spec.get("name") or "index")
    if name.lower() in existing:
        return "skipped", "already present"
    fragment, warning = index_definition(spec, coltypes, dialect)
    if not fragment:
        return "skipped", warning or "no such columns on the copy"
    connection.execute(text(f"ALTER TABLE {qualified} ADD {fragment}"))
    return "applied", warning


def ensure_indexes(
    database: Database,
    table: str,
    schema: str | None,
    indexes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create the primary key and indexes a source table declares.

    ``indexes`` is the list the plugin built from the source database:
    ``[{"name": "PRIMARY", "primary": true, "unique": true, "columns": ["id"]},
    {"name": "mdl_gradgrad_use_ix", "unique": false, "columns": ["userid"]}]``.

    Idempotent: anything already present is reported as skipped, so it is safe to
    re-run for every table after each sync.
    """
    clean = re.sub(r"[^a-z0-9_]", "_", (table or "").lower())
    if not clean:
        raise ValueError("table name is required")

    applied: list[str] = []
    skipped: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []

    with database.get_sqla_engine(schema=schema) as engine:
        dialect = (engine.dialect.name or "").lower()
        inspector = inspect(engine)
        columninfo = inspector.get_columns(clean, schema=schema)
        columns = {column["name"] for column in columninfo}
        coltypes = {str(column["name"]): str(column["type"]) for column in columninfo}
        existing = {
            str(index.get("name")).lower()
            for index in inspector.get_indexes(clean, schema=schema)
        }
        primary = inspector.get_pk_constraint(clean, schema=schema) or {}
        has_primary = bool(primary.get("constrained_columns"))

        qualified = _quote(clean, dialect)
        if schema:
            qualified = f"{_quote(schema, dialect)}.{qualified}"

        for spec in indexes or []:
            name = str(spec.get("name") or "index")
            wanted = [str(column) for column in (spec.get("columns") or [])]
            present = [column for column in wanted if column in columns]
            if not present:
                skipped.append({"name": name, "why": "no such columns on the copy"})
                continue
            columnlist = ", ".join(_quote(column, dialect) for column in present)

            try:
                with engine.begin() as connection:
                    if spec.get("primary"):
                        if has_primary:
                            skipped.append({"name": "PRIMARY", "why": "already present"})
                            continue
                        connection.execute(
                            text(f"ALTER TABLE {qualified} ADD PRIMARY KEY ({columnlist})")
                        )
                        applied.append("PRIMARY")
                        has_primary = True
                        continue

                    outcome, note = _add_secondary_index(
                        connection, qualified, spec, coltypes, dialect, existing
                    )
                    if outcome == "applied":
                        applied.append(name)
                    if note:
                        skipped.append({"name": name, "why": note})
            except Exception as ex:  # pylint: disable=broad-except
                failed.append({"name": name, "error": str(ex)[:200]})

    return {
        "table": clean,
        "applied": applied,
        "skipped": skipped,
        "failed": failed,
    }
